"""Playwright workflow for submitting NextRequest public records requests."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import SplitResult, urlsplit, urlunsplit

from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

from config import ACCOUNT_PASSWORD, DEPARTMENT_PRIORITY, REQUEST_TEMPLATE, SCREENSHOT_DIR, RequesterProfile


SEMANTIC_TARGETS = {
    "request_description",
    "email",
    "name",
    "phone",
    "street_address",
    "city",
    "state",
    "zip",
    "company",
    "department",
    "consent_checkbox",
    "submit_button",
    "ignore",
}


class RequestSubmitter:
    """Encapsulates the portal submission flow for a single municipality row."""

    def __init__(self, page: Page, profile: RequesterProfile, ai_mapper) -> None:
        self.page = page
        self.profile = profile
        self.ai_mapper = ai_mapper

    def submit(self, municipality: str, portal_url: str) -> Dict:
        try:
            self._open_request_form(portal_url)
            self._fill_form_with_page_scanning(municipality)
            self._create_account_if_prompted()
            request_number = self._extract_request_number()
            return {
                "status": "submitted",
                "submitted_at": datetime.now(timezone.utc).isoformat(),
                "request_number": request_number,
                "failed": False,
                "records_received": False,
                "notes": "Request submitted successfully",
                "screenshot_path": "",
            }
        except Exception as exc:
            screenshot_path = self._safe_failure_screenshot(municipality)
            return {
                "status": "failed",
                "submitted_at": datetime.now(timezone.utc).isoformat(),
                "request_number": "",
                "failed": True,
                "records_received": False,
                "notes": f"{type(exc).__name__}: {exc}",
                "screenshot_path": str(screenshot_path) if screenshot_path else "",
            }

    # -------------------------
    # Navigation to form
    # -------------------------
    def _open_request_form(self, portal_url: str) -> None:
        normalized_portal_url = portal_url.strip()
        request_url = self._build_request_form_url(normalized_portal_url)

        print(f"[request_submitter] portal_url: {normalized_portal_url}")
        print(f"[request_submitter] trying direct form URL: {request_url}")

        try:
            self.page.goto(request_url, wait_until="domcontentloaded", timeout=60_000)
            self._wait_for_form_readiness()
            direct_ok, direct_reason = self._is_on_request_form()
            print(f"[request_submitter] direct navigation succeeded: {direct_ok} ({direct_reason})")
            if direct_ok:
                return
        except Exception as exc:
            print(f"[request_submitter] direct navigation failed with exception: {type(exc).__name__}: {exc}")

        print("[request_submitter] attempting fallback navigation on portal page")
        self.page.goto(normalized_portal_url, wait_until="domcontentloaded", timeout=60_000)
        clicked_desc = self._click_make_request_fallback_link()
        print(f"[request_submitter] fallback clicked: {clicked_desc}")
        print(f"[request_submitter] final page URL after fallback click: {self.page.url}")

        self._wait_for_form_readiness()
        is_form, reason = self._is_on_request_form()
        if not is_form:
            print(f"[request_submitter] request form validation failed: {reason}")
            print(f"[request_submitter] final page URL: {self.page.url}")
            raise RuntimeError("Reached portal but could not open a valid request form page.")

    def _build_request_form_url(self, portal_url: str) -> str:
        parsed = urlsplit(portal_url.strip())
        path = (parsed.path or "").rstrip("/")
        if not path.endswith("/requests/new"):
            path = f"{path}/requests/new" if path else "/requests/new"
        return urlunsplit(SplitResult(parsed.scheme, parsed.netloc, path, "", ""))

    def _wait_for_form_readiness(self) -> None:
        try:
            self.page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightTimeoutError:
            pass
        self.page.wait_for_timeout(1200)

    def _is_on_request_form(self) -> Tuple[bool, str]:
        url_has_new = "/requests/new" in self.page.url.lower()
        text_checks = {
            "request_description_text": self.page.get_by_text(re.compile(r"request\s*description", re.I)).count() > 0,
            "your_information_text": self.page.get_by_text(re.compile(r"your\s*information", re.I)).count() > 0,
            "email_text": self.page.get_by_text(re.compile(r"\bemail\b", re.I)).count() > 0,
            "name_text": self.page.get_by_text(re.compile(r"\bname\b", re.I)).count() > 0,
        }
        positives = sum(1 for ok in text_checks.values() if ok)
        valid = url_has_new or positives >= 2
        return valid, f"url_has_new={url_has_new}, text_hits={positives}, checks={text_checks}"

    def _click_make_request_fallback_link(self) -> str:
        candidates = self._collect_request_entry_candidates()
        print("[request_submitter] request-entry candidates found:")
        for c in candidates:
            print(f"  - id={c['candidate_id']} text={c.get('text')} href={c.get('href')}")

        if not candidates:
            raise RuntimeError("No request-entry candidates found on portal landing page.")

        ranked = sorted(candidates, key=self._rule_score_candidate, reverse=True)
        selected = ranked[0]
        best_score = self._rule_score_candidate(selected)
        print(f"[request_submitter] rule-selected candidate: id={selected['candidate_id']} score={best_score}")

        used_ai = False
        if best_score < 90 and hasattr(self.ai_mapper, "choose_request_entry_candidate"):
            print("[request_submitter] OpenAI fallback used for request-entry selection")
            ai_selected = self.ai_mapper.choose_request_entry_candidate(candidates)
            if ai_selected:
                ai_candidate = next((c for c in candidates if c["candidate_id"] == ai_selected), None)
                if ai_candidate:
                    selected = ai_candidate
                    used_ai = True
                    print(f"[request_submitter] OpenAI selected candidate id={ai_selected}")

        if not self._click_candidate_by_id(selected["candidate_id"]):
            raise RuntimeError(f"Failed to click selected request-entry candidate id={selected['candidate_id']}")
        return f"candidate_id={selected['candidate_id']} source={'openai' if used_ai else 'rules'}"

    def _collect_request_entry_candidates(self) -> List[Dict]:
        return self.page.evaluate(
            """
            () => {
              const selectors = ['a','button','[role="button"]','[role="link"]','nav a','.card a','.tile a'];
              const nodes = Array.from(new Set(Array.from(document.querySelectorAll(selectors.join(',')))));
              const out = [];
              let idx = 1;
              for (const el of nodes) {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                const visible = style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
                if (!visible) continue;
                const disabled = ('disabled' in el && el.disabled) || el.hasAttribute('disabled') || (el.getAttribute('aria-disabled') || '').toLowerCase() === 'true';
                if (disabled) continue;
                const text = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 250);
                const href = (el.getAttribute('href') || '').trim();
                const aria = (el.getAttribute('aria-label') || '').trim();
                const title = (el.getAttribute('title') || '').trim();
                if (!text && !href && !aria && !title) continue;
                const id = `nr_candidate_${idx++}`;
                el.setAttribute('data-nr-candidate-id', id);
                out.push({candidate_id:id, tag:el.tagName.toLowerCase(), text, href, aria_label:aria, title});
              }
              return out;
            }
            """
        )

    @staticmethod
    def _rule_score_candidate(candidate: Dict) -> int:
        blob = " ".join(
            [str(candidate.get("text", "")), str(candidate.get("aria_label", "")), str(candidate.get("title", ""))]
        ).lower()
        href = str(candidate.get("href", "")).lower()
        score = 0
        if "/requests/new" in href:
            score += 120
        if "make a new public records request" in blob:
            score += 100
        if "make request" in blob:
            score += 80
        if "new request" in blob:
            score += 70
        if "public records request" in blob:
            score += 60
        return score

    def _click_candidate_by_id(self, candidate_id: str) -> bool:
        try:
            return bool(
                self.page.evaluate(
                    """
                    (cid) => {
                      const el = document.querySelector(`[data-nr-candidate-id="${cid}"]`);
                      if (!el) return false;
                      el.scrollIntoView({block:'center', inline:'center'});
                      el.click();
                      return true;
                    }
                    """,
                    candidate_id,
                )
            )
        except Exception:
            return False

    # -------------------------
    # Scan + classify + fill + submit
    # -------------------------
    def _fill_form_with_page_scanning(self, municipality: str) -> None:
        interactables = self._scan_interactable_elements()
        print(f"[request_submitter] interactables scanned: {len(interactables)}")

        mapping = self._classify_interactables(interactables)
        value_map = self._build_value_map(municipality)
        unmet_required = self._apply_interactable_actions(interactables, mapping, value_map)

        interactables_after = self._scan_interactable_elements()
        mapping_after = self._classify_interactables(interactables_after)
        self._debug_submit_candidates(interactables_after, mapping_after)

        post_fill_validation = self._collect_post_fill_validation(interactables_after)
        submit_enabled_before = self._count_enabled_submit_controls(interactables_after)
        print(
            "[request_submitter] post-fill validation "
            f"remaining_required={post_fill_validation.get('remaining_required_ids')} "
            f"validation_messages={post_fill_validation.get('validation_messages')} "
            f"invalid_elements={post_fill_validation.get('invalid_elements')}"
        )

        # Re-scan once after possible required boolean/checkbox state changes to verify submit availability
        self.page.wait_for_timeout(400)
        interactables_ready = self._scan_interactable_elements()
        submit_enabled_after = self._count_enabled_submit_controls(interactables_ready)
        print(
            "[request_submitter] submit enabled state "
            f"before={submit_enabled_before} after={submit_enabled_after}"
        )
        mapping_ready = self._classify_interactables(interactables_ready)

        if submit_enabled_after <= 0:
            validation_summary = self._collect_validation_summary()
            raise RuntimeError(
                "Submit not attempted because no enabled submit controls were found after required checkbox/radio handling. "
                f"Validation summary: {validation_summary}"
            )

        clicked, click_meta = self._click_real_submit(interactables_ready, mapping_ready)
        print(
            "[request_submitter] submit click result "
            f"clicked={clicked} meta={click_meta}"
        )
        if not clicked:
            validation_summary = self._collect_validation_summary()
            raise RuntimeError(
                "Could not find enabled submit action after filling form and consent elements. "
                f"Unmet required fields: {unmet_required}. "
                f"Post-fill validation: {post_fill_validation}. "
                f"Validation summary: {validation_summary}"
            )

        success, reason = self._confirm_submission_success(click_meta)
        if not success:
            raise RuntimeError(f"Submit button clicked but no success transition detected. {reason}")
        print(f"[request_submitter] submission success: {reason}")

    def _scan_interactable_elements(self) -> List[Dict]:
        return self.page.evaluate(
            """
            () => {
              const selectors = 'input, textarea, select, button, a, [role="button"], [contenteditable="true"]';
              const nodes = Array.from(document.querySelectorAll(selectors));

              const isVisible = (el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
              };

              const out = [];
              let seq = 1;
              for (const el of nodes) {
                if (!isVisible(el)) continue;
                const tag = el.tagName.toLowerCase();
                const type = (el.getAttribute('type') || '').toLowerCase();
                if (tag === 'input' && type === 'hidden') continue;
                if (el.hasAttribute('data-formula') || el.hasAttribute('data-video') || el.hasAttribute('data-link')) continue;
                if (el.classList.contains('ql-clipboard') || el.closest('.ql-hidden, .ql-clipboard, .ql-tooltip')) continue;

                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                const visible = style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
                const className = (el.className || '').toString();
                const clsLower = className.toLowerCase();
                const disabledByClass = clsLower.includes('disabled') || clsLower.includes('is-disabled') || clsLower.includes('btn-disabled');
                const disabled = ('disabled' in el && el.disabled) || el.hasAttribute('disabled') || (el.getAttribute('aria-disabled') || '').toLowerCase() === 'true' || disabledByClass;
                const enabled = !disabled;

                const id = el.id || '';
                let labelText = '';
                if (id) {
                  const lbl = document.querySelector(`label[for="${id}"]`);
                  if (lbl) labelText = (lbl.innerText || lbl.textContent || '').replace(/\s+/g, ' ').trim();
                }
                if (!labelText) {
                  const wrap = el.closest('label');
                  if (wrap) labelText = (wrap.innerText || wrap.textContent || '').replace(/\s+/g, ' ').trim();
                }
                const container = el.closest('.field, .form-group, .control-group, .nr-form-group, .nr-form-row, .card, .panel') || el.parentElement;
                const nearby = container ? (container.innerText || container.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 300) : '';
                const requiredAttr = !!(el.required || el.getAttribute('aria-required') === 'true');
                const requiredByText = /\*|required/i.test(`${labelText} ${nearby}`);
                const required = requiredAttr || requiredByText;
                const checked = (type === 'checkbox' || type === 'radio') ? !!el.checked : false;
                const editable = !!(el.isContentEditable || tag === 'textarea' || tag === 'select' || (tag === 'input' && !['checkbox','radio','button','submit'].includes(type))) && !el.readOnly;

                const elId = `nr_form_el_${seq++}`;
                el.setAttribute('data-nr-form-el-id', elId);

                out.push({
                  element_id: elId,
                  tag,
                  type,
                  label_text: labelText,
                  nearby_text: nearby,
                  placeholder: (el.getAttribute('placeholder') || '').trim(),
                  name: (el.getAttribute('name') || '').trim(),
                  id,
                  aria_label: (el.getAttribute('aria-label') || '').trim(),
                  aria_disabled: (el.getAttribute('aria-disabled') || '').trim(),
                  class_name: className.slice(0, 250),
                  text_content: (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 250),
                  href: (el.getAttribute('href') || '').trim(),
                  role: (el.getAttribute('role') || '').trim(),
                  inside_form: !!el.closest('form, .new_request, #new_request, .request-form, [data-testid*="request" i], .nr-form'),
                  in_nav: !!el.closest('nav, header, .navbar, .top-nav, .tabs, .tab-nav, [role="navigation"]'),
                  in_footer: !!el.closest('footer, .footer, .site-footer'),
                  y_position: Math.round(rect.top + window.scrollY),
                  visible,
                  enabled,
                  editable,
                  checked,
                  required,
                });
              }
              return out;
            }
            """
        )

    def _classify_interactables(self, interactables: List[Dict]) -> Dict[str, str]:
        mapping: Dict[str, str] = {}
        unclear: List[Dict] = []
        for e in interactables:
            label = self._classify_by_rules(e)
            mapping[e["element_id"]] = label
            if label == "ignore":
                unclear.append(e)

        if unclear and hasattr(self.ai_mapper, "classify_interactable_elements"):
            ai_mapping = self.ai_mapper.classify_interactable_elements(unclear)
            for element_id, label in ai_mapping.items():
                if label in SEMANTIC_TARGETS:
                    mapping[element_id] = label
        return mapping

    def _classify_by_rules(self, element: Dict) -> str:
        tag = str(element.get("tag", "")).lower()
        input_type = str(element.get("type", "")).lower()
        blob = " ".join(
            [
                str(element.get("label_text", "")),
                str(element.get("nearby_text", "")),
                str(element.get("placeholder", "")),
                str(element.get("name", "")),
                str(element.get("id", "")),
                str(element.get("aria_label", "")),
                str(element.get("text_content", "")),
            ]
        ).lower()

        if tag in {"button", "a"} or input_type in {"submit", "button"}:
            if any(k in blob for k in ["make request", "submit", "send request", "create request"]):
                return "submit_button"
            return "ignore"

        if input_type in {"checkbox", "radio"}:
            if any(k in blob for k in ["acknowledge", "agree", "terms", "conditions", "disclaimer", "reviewed", "consent", "accept"]):
                return "consent_checkbox"
            return "ignore"

        if any(k in blob for k in ["request description", "describe your request", "description", "details of request"]):
            return "request_description"
        if "email" in blob:
            return "email"
        if re.search(r"\b(full\s*)?name\b", blob):
            return "name"
        if any(k in blob for k in ["phone", "telephone", "tel"]):
            return "phone"
        if any(k in blob for k in ["street address", "mailing address", "address line"]):
            return "street_address"
        if re.search(r"\bcity\b", blob):
            return "city"
        if re.search(r"\bstate\b", blob):
            return "state"
        if any(k in blob for k in ["zip", "postal"]):
            return "zip"
        if any(k in blob for k in ["company", "organization", "employer"]):
            return "company"
        if "department" in blob:
            return "department"

        return "ignore"

    def _apply_interactable_actions(self, interactables: List[Dict], mapping: Dict[str, str], value_map: Dict[str, str]) -> List[str]:
        required_elements = [e for e in interactables if e.get("required")]
        print("[request_submitter] required fields detected:")
        for el in required_elements:
            print(
                f"  - id={el['element_id']} tag={el.get('tag')} type={el.get('type')} label={el.get('label_text')} "
                f"name={el.get('name')} mapping={mapping.get(el['element_id'])}"
            )

        filled_required: set[str] = set()
        unmet_required: List[str] = []
        consumed = set()

        for label in ["request_description", "email", "name", "phone", "street_address", "city", "state", "zip", "company", "department"]:
            value = value_map.get(label)
            for element in interactables:
                el_id = element["element_id"]
                if mapping.get(el_id) != label:
                    continue
                if label != "request_description" and label in consumed:
                    continue

                success = False
                if label == "department":
                    success = self._set_department_by_element_id(el_id)
                elif label == "state":
                    success = self._set_state_by_element_id(el_id)
                else:
                    if value:
                        success = self._fill_element_by_id(el_id, value)

                if success:
                    consumed.add(label)
                    if element.get("required"):
                        filled_required.add(el_id)
                    break

                if element.get("required"):
                    reason = f"{el_id}: required {label} fill failed"
                    if label in {"state", "department"}:
                        reason = f"{el_id}: required {label} fill failed (dropdown/combobox selection failed)"
                    unmet_required.append(reason)

        consent_elements = [e for e in interactables if mapping.get(e["element_id"]) == "consent_checkbox"]
        print("[request_submitter] consent checkboxes detected:")
        for element in consent_elements:
            el_id = element["element_id"]
            was_checked = bool(element.get("checked"))
            final_checked = was_checked
            if not was_checked and (element.get("required") or self._looks_like_consent_text(element)):
                final_checked = self._check_element_by_id(el_id)
            print(
                f"  - id={el_id} required={element.get('required')} checked_before={was_checked} checked_after={final_checked} "
                f"text={element.get('label_text') or element.get('nearby_text') or element.get('text_content')}"
            )
            if element.get("required") and final_checked:
                filled_required.add(el_id)
            elif element.get("required") and not final_checked:
                unmet_required.append(f"{el_id}: required consent checkbox could not be checked")

        # Required boolean controls: detect all required/disclaimer checkbox+radio gates
        all_bool_controls = [
            e for e in interactables if str(e.get("type", "")).lower() in {"checkbox", "radio"}
        ]
        print("[request_submitter] all checkbox/radio inputs found:")
        for element in all_bool_controls:
            print(
                f"  - id={element.get('element_id')} checked={element.get('checked')} required_flag={element.get('required')} "
                f"label={self._extract_boolean_label_text(element)}"
            )

        required_bool_controls: List[Tuple[Dict, List[str]]] = []
        for element in all_bool_controls:
            reasons = self._required_boolean_reasons(element)
            if reasons:
                required_bool_controls.append((element, reasons))

        print("[request_submitter] required checkbox/radio controls found:")
        for element, reasons in required_bool_controls:
            print(
                f"  - id={element.get('element_id')} checked={element.get('checked')} reasons={reasons} "
                f"label={self._extract_boolean_label_text(element)}"
            )

        checked_ids: List[str] = []
        skipped_ids: List[str] = []
        for element, _reasons in required_bool_controls:
            el_id = element["element_id"]
            if bool(element.get("checked")):
                filled_required.add(el_id)
                skipped_ids.append(f"{el_id}: already_checked")
                continue

            selected = self._check_element_by_id(el_id)
            print(f"[request_submitter] required boolean selected id={el_id} selected={selected}")
            if selected:
                filled_required.add(el_id)
                checked_ids.append(el_id)
            else:
                unmet_required.append(f"{el_id}: required boolean checkbox/radio could not be selected")
                skipped_ids.append(f"{el_id}: selection_failed")

        print(f"[request_submitter] required boolean checked: {checked_ids}")
        print(f"[request_submitter] required boolean skipped: {skipped_ids}")

        if filled_required:
            print("[request_submitter] required fields filled:")
            for el_id in sorted(filled_required):
                print(f"  - {el_id}")
        if unmet_required:
            print("[request_submitter] required fields skipped:")
            for msg in unmet_required:
                print(f"  - {msg}")

        return unmet_required

    def _set_state_by_element_id(self, element_id: str) -> bool:
        locator = self.page.locator(f"[data-nr-form-el-id='{element_id}']")
        for item in locator.all():
            try:
                tag = item.evaluate("el => el.tagName.toLowerCase()")
                if tag == "select":
                    before = item.evaluate("el => el.value || ''")
                    options = item.evaluate(
                        """
                        el => Array.from(el.options).map(o => ({value:(o.value||'').trim(), text:(o.textContent||'').trim()}))
                        """
                    )
                    targets = ["CA", "California"]
                    chosen = None
                    for target in targets:
                        for opt in options:
                            if opt["text"].lower() == target.lower() or opt["value"].lower() == target.lower():
                                chosen = opt
                                break
                        if chosen:
                            break
                    if chosen:
                        try:
                            item.select_option(label=chosen["text"])
                        except Exception:
                            item.select_option(value=chosen["value"])
                        after = item.evaluate("el => el.value || ''")
                        print(f"[request_submitter] state select chosen={chosen} final_value={after}")
                        if after and after != before:
                            return True

                # combobox/input fallback
                before = item.evaluate("el => ('value' in el ? el.value : el.innerText) || ''")
                for candidate in ["CA", "California"]:
                    if self._fill_locator_value(item, candidate):
                        after = item.evaluate("el => ('value' in el ? el.value : el.innerText) || ''")
                        print(f"[request_submitter] state combobox chosen={candidate} final_value={after}")
                        if after and after != before:
                            return True
            except Exception:
                continue
        return False

    def _set_department_by_element_id(self, element_id: str) -> bool:
        locator = self.page.locator(f"[data-nr-form-el-id='{element_id}']")
        direct_priorities = ["Controller", "Tax", "Finance", "Clerk"]
        variant_targets = [
            "Controller Department",
            "Tax Department",
            "Finance Department",
            "Clerk Department",
            "City Clerk",
            "Records",
            "Public Records",
            "Administration",
            "Administrative Services",
            "Finance and Administration",
            "Auditor Controller",
            "Treasurer Tax Collector",
            "Revenue",
            "Accounting",
        ]
        all_targets = direct_priorities + variant_targets
        semantic_keywords = ["controller", "tax", "finance", "clerk", "records", "administration"]

        for item in locator.all():
            try:
                tag = item.evaluate("el => el.tagName.toLowerCase()")
                options = self._get_visible_department_options(item)
                if options:
                    print(f"[request_submitter] available department options: {options}")

                if tag == "select":
                    # Step A+B direct/variant selection in select
                    for target in all_targets:
                        chosen = next((o for o in options if target.lower() in o.lower()), None)
                        if chosen and self._select_department_option_from_control(item, chosen):
                            print(f"[request_submitter] final chosen department={chosen} via direct match")
                            return True

                    # Step C fuzzy selection
                    fuzzy_choice = self._fuzzy_choose_department_option(options, all_targets, semantic_keywords)
                    if fuzzy_choice and self._select_department_option_from_control(item, fuzzy_choice["option"]):
                        print(
                            "[request_submitter] fuzzy match result "
                            f"chosen={fuzzy_choice['option']} score={fuzzy_choice['score']:.3f}"
                        )
                        print(f"[request_submitter] final chosen department={fuzzy_choice['option']} via fuzzy")
                        return True

                    # Step D OpenAI fallback
                    ai_choice = self._ai_choose_department_option(options)
                    if ai_choice and self._select_department_option_from_control(item, ai_choice):
                        print(f"[request_submitter] OpenAI fallback result={ai_choice}")
                        print(f"[request_submitter] final chosen department={ai_choice} via openai")
                        return True

                # combobox/searchable flow for non-select controls
                # Step A+B direct text attempts
                for target in all_targets:
                    before_display = self._safe_locator_value(item)
                    before_hidden = self._infer_hidden_combobox_value(item)
                    result = self._search_and_select_combobox(item, target, before_display, before_hidden)
                    print(
                        "[request_submitter] department combobox "
                        f"candidate={target} result={result.get('selected')} "
                        f"display={result.get('display_value')} hidden={result.get('hidden_value')}"
                    )
                    if result.get("selected"):
                        print(f"[request_submitter] final chosen department={result.get('display_value') or target} via combobox")
                        return True

                # Step C fuzzy against visible popup/list options
                dynamic_options = self._get_global_visible_option_texts()
                if dynamic_options:
                    print(f"[request_submitter] available department options: {dynamic_options}")
                fuzzy_choice = self._fuzzy_choose_department_option(dynamic_options, all_targets, semantic_keywords)
                if fuzzy_choice:
                    before_display = self._safe_locator_value(item)
                    before_hidden = self._infer_hidden_combobox_value(item)
                    result = self._search_and_select_combobox(item, fuzzy_choice["option"], before_display, before_hidden)
                    print(
                        "[request_submitter] fuzzy match result "
                        f"chosen={fuzzy_choice['option']} score={fuzzy_choice['score']:.3f} selected={result.get('selected')}"
                    )
                    if result.get("selected"):
                        print(f"[request_submitter] final chosen department={fuzzy_choice['option']} via fuzzy combobox")
                        return True

                # Step D OpenAI fallback for combobox
                ai_choice = self._ai_choose_department_option(dynamic_options or options)
                if ai_choice:
                    before_display = self._safe_locator_value(item)
                    before_hidden = self._infer_hidden_combobox_value(item)
                    result = self._search_and_select_combobox(item, ai_choice, before_display, before_hidden)
                    print(f"[request_submitter] OpenAI fallback result={ai_choice} selected={result.get('selected')}")
                    if result.get("selected"):
                        print(f"[request_submitter] final chosen department={ai_choice} via openai combobox")
                        return True
            except Exception:
                continue
        return False

    def _get_visible_department_options(self, locator: Locator) -> List[str]:
        try:
            tag = locator.evaluate("el => el.tagName.toLowerCase()")
            if tag != "select":
                return []
            options = locator.evaluate(
                """
                el => Array.from(el.options)
                  .map(o => (o.textContent || '').replace(/\s+/g,' ').trim())
                  .filter(Boolean)
                """
            )
            return [o for o in options if o and o.lower() not in {"select", "choose"}]
        except Exception:
            return []

    def _get_global_visible_option_texts(self) -> List[str]:
        try:
            options = self.page.evaluate(
                """
                () => {
                  const sels = ['[role="listbox"] [role="option"]','[role="menu"] [role="menuitem"]','li[role="option"]','li','.select-option','.dropdown-item','.menu-item'];
                  const nodes = Array.from(new Set(Array.from(document.querySelectorAll(sels.join(',')))));
                  const visible = nodes.filter(el => {
                    const s = window.getComputedStyle(el);
                    const r = el.getBoundingClientRect();
                    return s.visibility !== 'hidden' && s.display !== 'none' && r.width > 0 && r.height > 0;
                  });
                  return visible.map(el => (el.innerText || el.textContent || '').replace(/\s+/g,' ').trim()).filter(Boolean).slice(0,60);
                }
                """
            )
            dedup = []
            for o in options:
                if o not in dedup:
                    dedup.append(o)
            return dedup
        except Exception:
            return []

    def _fuzzy_choose_department_option(
        self, options: List[str], targets: List[str], semantic_keywords: List[str]
    ) -> Optional[Dict[str, Any]]:
        if not options:
            return None
        best: Optional[Dict[str, Any]] = None
        for opt in options:
            opt_l = opt.lower()
            semantic_bonus = 0.08 if any(k in opt_l for k in semantic_keywords) else 0.0
            for t in targets:
                score = SequenceMatcher(None, opt_l, t.lower()).ratio() + semantic_bonus
                if best is None or score > best["score"]:
                    best = {"option": opt, "target": t, "score": score}
        if best and best["score"] >= 0.55:
            return best
        return None

    def _ai_choose_department_option(self, options: List[str]) -> Optional[str]:
        if not options:
            return None
        if hasattr(self.ai_mapper, "choose_department_option"):
            try:
                return self.ai_mapper.choose_department_option(options)
            except Exception:
                return None
        return None

    def _select_department_option_from_control(self, locator: Locator, option_text: str) -> bool:
        try:
            tag = locator.evaluate("el => el.tagName.toLowerCase()")
            before_display = self._safe_locator_value(locator)
            before_hidden = self._infer_hidden_combobox_value(locator)
            if tag == "select":
                locator.select_option(label=option_text)
                self._dispatch_field_events(locator)
                after_display = self._safe_locator_value(locator)
                after_hidden = self._infer_hidden_combobox_value(locator)
                return bool(
                    (after_display and after_display != before_display)
                    or (after_hidden and after_hidden != before_hidden)
                    or option_text.lower() in f"{after_display} {after_hidden}".lower()
                )
            result = self._search_and_select_combobox(locator, option_text, before_display, before_hidden)
            return bool(result.get("selected"))
        except Exception:
            return False

    def _search_and_select_combobox(
        self, locator: Locator, text: str, before_display: str = "", before_hidden: str = ""
    ) -> Dict[str, Any]:
        result: Dict[str, Any] = {"selected": False, "display_value": "", "hidden_value": "", "changed": False}
        try:
            locator.scroll_into_view_if_needed(timeout=2000)
            locator.click(timeout=2000)
            self._fill_locator_value(locator, text)
            self.page.wait_for_timeout(700)

            option_candidates = [
                self.page.get_by_role("option", name=re.compile(re.escape(text), re.I)),
                self.page.get_by_role("option").filter(has_text=re.compile(re.escape(text), re.I)),
                self.page.locator("[role='listbox'] [role='option'], [aria-expanded='true'] [role='option']").filter(
                    has_text=re.compile(re.escape(text), re.I)
                ),
                self.page.locator("li, [role='option'], .select-option, .dropdown-item, .menu-item").filter(
                    has_text=re.compile(re.escape(text), re.I)
                ),
            ]
            clicked_option = False
            for options in option_candidates:
                for opt in options.all():
                    try:
                        if not opt.is_visible():
                            continue
                        opt.scroll_into_view_if_needed(timeout=1000)
                        opt.click(timeout=2000)
                        clicked_option = True
                        break
                    except Exception:
                        continue
                if clicked_option:
                    break

            if not clicked_option:
                try:
                    locator.press("ArrowDown")
                    locator.press("Enter")
                    clicked_option = True
                except Exception:
                    pass

            self.page.wait_for_timeout(500)
            display_value = self._safe_locator_value(locator)
            hidden_value = self._infer_hidden_combobox_value(locator)
            display_changed = bool(display_value and display_value.strip() != (before_display or "").strip())
            hidden_changed = bool(hidden_value and hidden_value.strip() != (before_hidden or "").strip())
            text_confirmed = text.lower() in f"{display_value} {hidden_value}".lower()
            changed = display_changed or hidden_changed

            result["display_value"] = display_value
            result["hidden_value"] = hidden_value
            result["changed"] = changed
            result["selected"] = bool(clicked_option and (changed or text_confirmed))
            return result
        except Exception:
            return result

    def _fill_locator_value(self, locator: Locator, value: str) -> bool:
        try:
            locator.focus(timeout=2000)
            try:
                locator.fill("")
            except Exception:
                try:
                    locator.press("Control+a")
                    locator.press("Backspace")
                except Exception:
                    pass
            locator.fill(value)
            self._dispatch_field_events(locator)
            return True
        except Exception:
            return False

    def _dispatch_field_events(self, locator: Locator) -> None:
        try:
            locator.dispatch_event("input")
        except Exception:
            pass
        try:
            locator.dispatch_event("change")
        except Exception:
            pass
        try:
            locator.evaluate("el => el.blur()")
        except Exception:
            pass

    def _safe_locator_value(self, locator: Locator) -> str:
        try:
            return str(locator.evaluate("el => ('value' in el ? el.value : el.innerText) || ''")).strip()
        except Exception:
            return ""

    def _infer_hidden_combobox_value(self, locator: Locator) -> str:
        try:
            return str(
                locator.evaluate(
                    """
                    (el) => {
                      const combo = el.closest('[role="combobox"], .select, .combobox, .nr-select, .react-select__control') || el.parentElement;
                      if (!combo) return '';
                      const hidden = combo.querySelector('input[type="hidden"], input[name*="department" i], input[id*="department" i]');
                      if (!hidden) return '';
                      return (hidden.value || '').toString().trim();
                    }
                    """
                )
            ).strip()
        except Exception:
            return ""

    def _looks_like_consent_text(self, element: Dict) -> bool:
        blob = " ".join([str(element.get("label_text", "")), str(element.get("nearby_text", "")), str(element.get("text_content", ""))]).lower()
        return any(k in blob for k in ["acknowledge", "agree", "terms", "conditions", "disclaimer", "reviewed", "consent", "accept"])

    def _extract_boolean_label_text(self, element: Dict) -> str:
        parts = [
            str(element.get("label_text", "")),
            str(element.get("aria_label", "")),
            str(element.get("nearby_text", "")),
            str(element.get("text_content", "")),
            str(element.get("name", "")),
            str(element.get("id", "")),
        ]
        return " ".join([p for p in parts if p]).strip()

    def _required_boolean_reasons(self, element: Dict) -> List[str]:
        reasons: List[str] = []
        label_blob = self._extract_boolean_label_text(element).lower()
        control_type = str(element.get("type", "")).lower()
        if control_type not in {"checkbox", "radio"}:
            return reasons

        if bool(element.get("required")):
            reasons.append("required_attr_or_marker")

        if any(k in label_blob for k in ["required", "must select", "at least one option must be selected"]):
            reasons.append("required_text_marker")

        if "*" in label_blob:
            reasons.append("asterisk_marker")

        disclaimer_keywords = [
            "police",
            "court",
            "not requesting",
            "not looking for",
            "confirm this is not",
            "disclaimer",
            "acknowledge",
            "certify",
        ]
        if any(k in label_blob for k in disclaimer_keywords):
            reasons.append("disclaimer_keyword")

        validation_blob = self._validation_text_near_element(str(element.get("element_id", ""))).lower()
        if validation_blob and any(k in validation_blob for k in ["required", "must select", "at least one option must be selected"]):
            reasons.append("nearby_validation_message")

        # Deduplicate while preserving order
        dedup: List[str] = []
        for r in reasons:
            if r not in dedup:
                dedup.append(r)
        return dedup

    def _validation_text_near_element(self, element_id: str) -> str:
        if not element_id:
            return ""
        try:
            return str(
                self.page.evaluate(
                    """
                    (id) => {
                      const el = document.querySelector(`[data-nr-form-el-id="${id}"]`);
                      if (!el) return '';
                      const container = el.closest('.field, .form-group, .control-group, .nr-form-group, .nr-form-row, .card, .panel') || el.parentElement;
                      if (!container) return '';
                      const nodes = Array.from(container.querySelectorAll('.error,.errors,.invalid-feedback,.field-error,.form-error,[role="alert"],[aria-live="assertive"],.validation-message'));
                      return nodes
                        .map(n => (n.innerText || n.textContent || '').replace(/\s+/g,' ').trim())
                        .filter(Boolean)
                        .slice(0, 6)
                        .join(' | ');
                    }
                    """,
                    element_id,
                )
            )
        except Exception:
            return ""

    def _count_enabled_submit_controls(self, interactables: List[Dict]) -> int:
        count = 0
        for e in interactables:
            tag = str(e.get("tag", "")).lower()
            input_type = str(e.get("type", "")).lower()
            role = str(e.get("role", "")).lower()
            inside_form = bool(e.get("inside_form"))
            blob = " ".join(
                [
                    str(e.get("label_text", "")),
                    str(e.get("nearby_text", "")),
                    str(e.get("aria_label", "")),
                    str(e.get("text_content", "")),
                ]
            ).lower()
            is_submit_wording = any(k in blob for k in ["make request", "submit", "send request", "create request"])
            is_control = tag == "button" or input_type == "submit" or (role == "button" and inside_form)
            if is_control and bool(e.get("enabled")) and bool(e.get("visible")) and is_submit_wording:
                count += 1
        return count

    def _debug_submit_candidates(self, interactables: List[Dict], mapping: Dict[str, str]) -> None:
        print("[request_submitter] submit candidates:")
        for e in interactables:
            blob = " ".join([str(e.get("label_text", "")), str(e.get("text_content", "")), str(e.get("aria_label", "")), str(e.get("name", ""))]).lower()
            if mapping.get(e["element_id"]) == "submit_button" or any(k in blob for k in ["make request", "submit", "send request"]):
                print(
                    f"  - id={e['element_id']} text={e.get('text_content') or e.get('label_text')} tag={e.get('tag')} "
                    f"enabled={e.get('enabled')} visible={e.get('visible')} aria-disabled={e.get('aria_disabled')} class={e.get('class_name')}"
                )

    def _click_real_submit(self, interactables: List[Dict], mapping: Dict[str, str]) -> Tuple[bool, Dict[str, Any]]:
        meta: Dict[str, Any] = {"clicked_element_id": "", "url_immediate": self.page.url}
        candidates = []

        for e in interactables:
            el_id = e["element_id"]
            text = str(e.get("text_content") or e.get("label_text") or "").strip()
            blob = " ".join(
                [
                    str(e.get("label_text", "")),
                    str(e.get("nearby_text", "")),
                    str(e.get("aria_label", "")),
                    str(e.get("text_content", "")),
                    str(e.get("name", "")),
                    str(e.get("id", "")),
                ]
            ).lower()
            tag = str(e.get("tag", "")).lower()
            role = str(e.get("role", "")).lower()
            href = str(e.get("href", "")).lower()
            input_type = str(e.get("type", "")).lower()
            inside_form = bool(e.get("inside_form"))
            in_nav = bool(e.get("in_nav"))
            in_footer = bool(e.get("in_footer"))
            enabled = bool(e.get("enabled"))
            visible = bool(e.get("visible"))

            score = 0
            reasons: List[str] = []
            rejected_reasons: List[str] = []

            is_submit_wording = any(k in blob for k in ["make request", "submit", "send request", "create request"])
            exact_submit_wording = text.lower() in {"make request", "submit", "send request"}
            looks_skip = any(k in blob for k in ["skip to main content", "skip to content", "back to top"])

            if not visible or not enabled:
                rejected_reasons.append("not_visible_or_enabled")
            if looks_skip:
                rejected_reasons.append("skip_link")
            if in_nav:
                rejected_reasons.append("in_navigation")
            if in_footer:
                rejected_reasons.append("in_footer")
            explicit_bad_link_texts = [
                "fee schedule",
                "public records policy",
                "help",
                "privacy",
                "terms",
                "documents",
                "all requests",
                "make request nav tab",
            ]
            if any(t in blob for t in explicit_bad_link_texts):
                rejected_reasons.append("non_submit_info_link")
            # only real submit controls
            is_control = tag == "button" or input_type == "submit" or (role == "button" and inside_form)
            if not is_control:
                rejected_reasons.append("not_submit_control")
            if tag == "a" and not (role == "button" and inside_form and is_submit_wording):
                rejected_reasons.append("anchor_not_explicit_submit_control")
            if tag == "a" and (href.startswith("#") or href.endswith("#") or "skip" in href):
                rejected_reasons.append("anchor_hash_or_skip_href")
            if not is_submit_wording and mapping.get(el_id) != "submit_button":
                rejected_reasons.append("non_submit_wording")

            if rejected_reasons:
                candidates.append(
                    {
                        "element": e,
                        "score": -999,
                        "reasons": reasons,
                        "rejected_reasons": rejected_reasons,
                    }
                )
                continue

            if tag == "button":
                score += 120
                reasons.append("button_tag")
            if input_type == "submit":
                score += 130
                reasons.append("input_submit")
            if role == "button":
                score += 90
                reasons.append("role_button")
            if inside_form:
                score += 80
                reasons.append("inside_form")
            if exact_submit_wording:
                score += 110
                reasons.append("exact_submit_text")
            elif is_submit_wording:
                score += 55
                reasons.append("submit_text_match")
            if mapping.get(el_id) == "submit_button":
                score += 30
                reasons.append("classified_submit")
            y_position = int(e.get("y_position") or 0)
            if y_position > 450:
                score += 35
                reasons.append("lower_page_position")
            if tag == "a":
                score -= 35
                reasons.append("anchor_penalty")
            if in_nav or in_footer:
                score -= 120
                reasons.append("nav_footer_penalty")

            candidates.append({"element": e, "score": score, "reasons": reasons, "rejected_reasons": rejected_reasons})

        ranked = sorted(candidates, key=lambda c: c["score"], reverse=True)
        print("[request_submitter] submit ranking:")
        for c in ranked[:12]:
            e = c["element"]
            print(
                "  - "
                f"id={e.get('element_id')} text={e.get('text_content') or e.get('label_text')} tag={e.get('tag')} "
                f"inside_form={bool(e.get('inside_form'))} score={c.get('score')} "
                f"why_chosen={c.get('reasons')} why_rejected={c.get('rejected_reasons')}"
            )

        for c in ranked:
            if c["score"] < 0:
                continue
            e = c["element"]
            el_id = e["element_id"]
            if self._click_element_by_id(el_id):
                meta["clicked_element_id"] = el_id
                meta["url_immediate"] = self.page.url
                meta["chosen_score"] = c["score"]
                meta["chosen_reasons"] = c["reasons"]
                meta["chosen_rejections"] = c["rejected_reasons"]
                return True, meta

        meta["disabled_submit_candidates"] = [
            e["element"]["element_id"] for e in ranked if bool(e["element"].get("inside_form")) and e["score"] < 0
        ]
        return False, meta

    def _confirm_submission_success(self, click_meta: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
        immediate_url = (click_meta or {}).get("url_immediate", self.page.url)
        clicked_element_id = (click_meta or {}).get("clicked_element_id", "")
        print(f"[request_submitter] submit clicked element={clicked_element_id}")
        print(f"[request_submitter] URL immediately after click: {immediate_url}")

        self._wait_for_form_readiness()
        waited_url = self.page.url
        body_text = self.page.inner_text("body").lower()

        success_indicators = [
            "request number",
            "your request has been submitted",
            "thank you",
            "confirmation",
            "request received",
            "successfully submitted",
            "we received your request",
        ]
        account_indicators = ["create account", "sign up", "set password", "create your account"]

        has_success_text = any(t in body_text for t in success_indicators)
        has_account_text = any(t in body_text for t in account_indicators)
        has_account_inputs = self.page.locator("input[type='password']:visible, input[name*='password' i]:visible").count() > 0
        has_email_input = self.page.locator("input[type='email']:visible, input[name*='email' i]:visible").count() > 0
        has_account_flow = has_account_text or (has_account_inputs and has_email_input)

        url_lower = waited_url.lower()
        request_new_remained_loaded = "/requests/new" in url_lower
        transitioned_url = not request_new_remained_loaded
        has_request_details_page = bool(re.search(r"/requests/(?!new(?:$|/))[A-Za-z0-9\-]+", waited_url, re.I))

        print(f"[request_submitter] URL after wait: {waited_url}")
        print(f"[request_submitter] account page appeared: {has_account_flow}")
        print(f"[request_submitter] success text appeared: {has_success_text}")
        print(f"[request_submitter] request/new remained loaded: {request_new_remained_loaded}")
        print(f"[request_submitter] request details page appeared: {has_request_details_page}")

        if transitioned_url or has_account_flow or has_success_text or has_request_details_page:
            return True, (
                f"immediate_url={immediate_url}, final_url={waited_url}, transitioned_url={transitioned_url}, "
                f"account_flow={has_account_flow}, success_text={has_success_text}, request_details={has_request_details_page}, "
                f"request_new_remained={request_new_remained_loaded}"
            )

        return False, (
            f"immediate_url={immediate_url}, final_url={waited_url}, transitioned_url={transitioned_url}, "
            f"account_flow={has_account_flow}, success_text={has_success_text}, request_details={has_request_details_page}, "
            f"request_new_remained={request_new_remained_loaded}"
        )

    def _collect_post_fill_validation(self, interactables: List[Dict]) -> Dict[str, Any]:
        summary = self.page.evaluate(
            """
            () => {
              const candidates = Array.from(document.querySelectorAll('[data-nr-form-el-id]'));
              const remainingRequired = [];
              const invalidElements = [];

              const isVisible = (el) => {
                const s = window.getComputedStyle(el);
                const r = el.getBoundingClientRect();
                return s.visibility !== 'hidden' && s.display !== 'none' && r.width > 0 && r.height > 0;
              };

              for (const el of candidates) {
                if (!isVisible(el)) continue;
                const id = el.getAttribute('data-nr-form-el-id') || '';
                const tag = el.tagName.toLowerCase();
                const type = (el.getAttribute('type') || '').toLowerCase();
                const required = !!(el.required || el.getAttribute('aria-required') === 'true');
                const value = (typeof el.value !== 'undefined' ? String(el.value || '').trim() : String(el.innerText || '').trim());
                const checked = (type === 'checkbox' || type === 'radio') ? !!el.checked : true;
                const invalidByAria = (el.getAttribute('aria-invalid') || '').toLowerCase() === 'true';
                const invalidByClass = /invalid|error|required|is-invalid/.test((el.className || '').toString().toLowerCase());

                if (required) {
                  const empty = (type === 'checkbox' || type === 'radio') ? !checked : !value;
                  if (empty) remainingRequired.push(id);
                }
                if (invalidByAria || invalidByClass) {
                  invalidElements.push({
                    id,
                    tag,
                    type,
                    name: el.getAttribute('name') || '',
                    aria_invalid: el.getAttribute('aria-invalid') || '',
                    class_name: (el.className || '').toString().slice(0, 120),
                    value: value.slice(0, 80),
                  });
                }
              }

              const msgSelectors = ['.error','.errors','.invalid-feedback','.field-error','.form-error','[role="alert"]','[aria-live="assertive"]','.validation-message'];
              const messages = Array.from(document.querySelectorAll(msgSelectors.join(',')))
                .filter(isVisible)
                .map(el => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim())
                .filter(Boolean)
                .slice(0, 10);

              return { remaining_required_ids: remainingRequired, validation_messages: messages, invalid_elements: invalidElements };
            }
            """
        )
        return {
            "remaining_required_ids": summary.get("remaining_required_ids", []),
            "validation_messages": summary.get("validation_messages", []),
            "invalid_elements": summary.get("invalid_elements", []),
        }

    def _collect_validation_summary(self) -> str:
        try:
            data = self.page.evaluate(
                """
                () => {
                  const selectors = ['.error','.errors','.invalid-feedback','.field-error','.form-error','[role="alert"]','[aria-live="assertive"]','.validation-message'];
                  const nodes = Array.from(document.querySelectorAll(selectors.join(',')));
                  const visibleText = nodes.filter(el => {
                    const s = window.getComputedStyle(el);
                    const r = el.getBoundingClientRect();
                    return s.visibility !== 'hidden' && s.display !== 'none' && r.width > 0 && r.height > 0;
                  }).map(el => (el.innerText || el.textContent || '').replace(/\s+/g,' ').trim()).filter(Boolean);
                  const bodyText = (document.body?.innerText || '').replace(/\s+/g,' ').trim();
                  return {
                    has_visible_validation_message: visibleText.length > 0,
                    validation_messages: visibleText.slice(0,8),
                    body_excerpt: bodyText.slice(0,900),
                  };
                }
                """
            )
            return f"has_visible_validation_message={data.get('has_visible_validation_message')}, messages={data.get('validation_messages')}, excerpt={data.get('body_excerpt')}"
        except Exception as exc:
            return f"unable to collect validation summary: {type(exc).__name__}: {exc}"

    def _fill_element_by_id(self, element_id: str, value: str) -> bool:
        try:
            return bool(
                self.page.evaluate(
                    """
                    ({id, value}) => {
                      const el = document.querySelector(`[data-nr-form-el-id="${id}"]`);
                      if (!el) return false;
                      const s = window.getComputedStyle(el);
                      const r = el.getBoundingClientRect();
                      const visible = s.visibility !== 'hidden' && s.display !== 'none' && r.width > 0 && r.height > 0;
                      const cls = (el.className || '').toString().toLowerCase();
                      const disabledByClass = cls.includes('disabled') || cls.includes('is-disabled') || cls.includes('btn-disabled');
                      const disabled = ('disabled' in el && el.disabled) || el.hasAttribute('disabled') || (el.getAttribute('aria-disabled') || '').toLowerCase() === 'true' || disabledByClass;
                      if (!visible || disabled) return false;

                      const tag = el.tagName.toLowerCase();
                      const type = (el.getAttribute('type') || '').toLowerCase();
                      if (tag === 'input' && ['checkbox','radio','submit','button','hidden'].includes(type)) return false;
                      if ('readOnly' in el && el.readOnly) return false;

                      el.scrollIntoView({block:'center', inline:'center'});
                      el.focus();

                      if (el.isContentEditable) {
                        el.innerText = value;
                      } else if (typeof el.value !== 'undefined') {
                        el.value = '';
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.value = value;
                      }

                      el.dispatchEvent(new Event('input', { bubbles: true }));
                      el.dispatchEvent(new Event('change', { bubbles: true }));
                      el.blur();
                      return true;
                    }
                    """,
                    {"id": element_id, "value": value},
                )
            )
        except Exception:
            return False

    def _check_element_by_id(self, element_id: str) -> bool:
        try:
            return bool(
                self.page.evaluate(
                    """
                    (id) => {
                      const el = document.querySelector(`[data-nr-form-el-id="${id}"]`);
                      if (!el) return false;
                      const type = (el.getAttribute('type') || '').toLowerCase();
                      if (type !== 'checkbox' && type !== 'radio') return false;
                      const s = window.getComputedStyle(el);
                      const r = el.getBoundingClientRect();
                      const visible = s.visibility !== 'hidden' && s.display !== 'none' && r.width > 0 && r.height > 0;
                      const disabled = ('disabled' in el && el.disabled) || el.hasAttribute('disabled') || (el.getAttribute('aria-disabled') || '').toLowerCase() === 'true';
                      if (!visible || disabled) return false;
                      if (el.checked) {
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        el.blur();
                        return true;
                      }
                      el.scrollIntoView({block:'center', inline:'center'});
                      el.click();
                      el.dispatchEvent(new Event('input', { bubbles: true }));
                      el.dispatchEvent(new Event('change', { bubbles: true }));
                      el.blur();
                      return !!el.checked;
                    }
                    """,
                    element_id,
                )
            )
        except Exception:
            return False

    def _click_element_by_id(self, element_id: str) -> bool:
        try:
            return bool(
                self.page.evaluate(
                    """
                    (id) => {
                      const el = document.querySelector(`[data-nr-form-el-id="${id}"]`);
                      if (!el) return false;
                      const s = window.getComputedStyle(el);
                      const r = el.getBoundingClientRect();
                      const visible = s.visibility !== 'hidden' && s.display !== 'none' && r.width > 0 && r.height > 0;
                      const cls = (el.className || '').toString().toLowerCase();
                      const disabledByClass = cls.includes('disabled') || cls.includes('is-disabled') || cls.includes('btn-disabled');
                      const disabled = ('disabled' in el && el.disabled) || el.hasAttribute('disabled') || (el.getAttribute('aria-disabled') || '').toLowerCase() === 'true' || disabledByClass;
                      if (!visible || disabled) return false;
                      el.scrollIntoView({block:'center', inline:'center'});
                      el.click();
                      return true;
                    }
                    """,
                    element_id,
                )
            )
        except Exception:
            return False

    def _build_value_map(self, municipality: str) -> Dict[str, str]:
        return {
            "request_description": REQUEST_TEMPLATE.format(municipality=municipality),
            "email": self.profile.email,
            "name": self.profile.full_name,
            "phone": self.profile.phone,
            "street_address": self.profile.address,
            "city": self.profile.city,
            "state": self.profile.state,
            "zip": self.profile.zip_code,
            "company": self.profile.company or self.profile.organization,
        }

    # -------------------------
    # Account / extraction / screenshot
    # -------------------------
    def _create_account_if_prompted(self) -> None:
        self.page.wait_for_timeout(2000)
        self._fill_first_fillable(self.page.locator("input[type='email']:visible, input[name*='email' i]:visible"), self.profile.email)

        password_locator = self.page.locator("input[type='password']:visible:not([readonly])")
        password_filled = 0
        for field in password_locator.all():
            if not self._is_fillable(field):
                continue
            field.fill(ACCOUNT_PASSWORD)
            password_filled += 1
            if password_filled >= 2:
                break

        if password_filled == 0:
            return

        create_btn = self.page.get_by_role("button", name=re.compile(r"(create|sign up|register|continue)", re.I))
        for button in create_btn.all():
            try:
                if not button.is_visible() or button.is_disabled():
                    continue
                button.click(timeout=10_000)
                return
            except Exception:
                continue

    def _extract_request_number(self) -> str:
        try:
            self.page.wait_for_load_state("networkidle", timeout=12_000)
        except PlaywrightTimeoutError:
            pass
        content = self.page.inner_text("body")
        for pattern in [
            r"request\s*(?:number|#|id)\s*[:#]?\s*([A-Z0-9\-]+)",
            r"tracking\s*(?:number|#)\s*[:#]?\s*([A-Z0-9\-]+)",
            r"#(\d{4,})",
        ]:
            match = re.search(pattern, content, flags=re.I)
            if match:
                return match.group(1)
        return ""

    def _safe_failure_screenshot(self, municipality: str) -> Optional[Path]:
        try:
            SCREENSHOT_DIR.mkdir(exist_ok=True)
            safe_name = re.sub(r"[^a-zA-Z0-9_-]+", "_", municipality).strip("_") or "municipality"
            filename = f"{safe_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            path = SCREENSHOT_DIR / filename
            self.page.screenshot(path=str(path), full_page=True)
            return path
        except Exception:
            return None

    # -------------------------
    # Shared helpers
    # -------------------------
    def _fill_first_fillable(self, locator: Locator, value: str) -> bool:
        for item in locator.all():
            if not self._is_fillable(item):
                continue
            try:
                item.scroll_into_view_if_needed(timeout=2_000)
                item.focus(timeout=2_000)
                item.fill(value, timeout=10_000)
                item.dispatch_event("input")
                item.dispatch_event("change")
                item.evaluate("el => el.blur()")
                return True
            except Exception:
                continue
        return False

    def _is_fillable(self, locator: Locator) -> bool:
        try:
            if not locator.is_visible() or locator.is_disabled():
                return False
            if not locator.is_editable() and locator.evaluate("el => el.tagName.toLowerCase() !== 'select'"):
                return False
            return bool(
                locator.evaluate(
                    """
                    (el) => {
                      const type = (el.getAttribute('type') || '').toLowerCase();
                      const cls = (el.className || '').toString().toLowerCase();
                      if (type === 'hidden') return false;
                      if (cls.includes('disabled') || cls.includes('is-disabled') || cls.includes('btn-disabled')) return false;
                      if (el.hasAttribute('data-formula') || el.hasAttribute('data-video') || el.hasAttribute('data-link')) return false;
                      if (el.classList.contains('ql-clipboard') || el.closest('.ql-hidden, .ql-clipboard, .ql-tooltip')) return false;
                      if ((el.tagName.toLowerCase() === 'input' || el.tagName.toLowerCase() === 'textarea') && el.readOnly) return false;
                      return true;
                    }
                    """
                )
            )
        except Exception:
            return False
