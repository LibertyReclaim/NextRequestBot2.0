"""OpenAI-powered classifiers for dynamic NextRequest automation."""

from __future__ import annotations

import json
from typing import Dict, List, Optional

from openai import OpenAI

SUPPORTED_TARGETS = {
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


class AIFieldMapper:
    """Maps discovered elements to semantic targets via OpenAI."""

    def __init__(self, api_key: str, model: str = "gpt-4.1-mini") -> None:
        if not api_key:
            raise ValueError("OPENAI_API_KEY is missing.")
        self.client = OpenAI(api_key=api_key)
        self.model = model

    def classify_fields(self, field_candidates: List[Dict]) -> Dict[str, str]:
        """Backward-compatible field classifier for input-like elements."""
        prompt = {
            "instructions": [
                "Classify each form field into exactly one target label.",
                "Allowed labels: request_description, email, name, phone, street_address, city, state, zip, company, department, ignore.",
                "If uncertain, use ignore.",
                "Output STRICT JSON object only: {\"mapping\": {\"<field_id>\": \"<label>\"}}",
            ],
            "fields": field_candidates,
        }

        completion = self.client.chat.completions.create(
            model=self.model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "You classify web form fields for robust automation."},
                {"role": "user", "content": json.dumps(prompt)},
            ],
        )

        payload = json.loads(completion.choices[0].message.content or "{}")
        mapping = payload.get("mapping", {})

        clean_mapping: Dict[str, str] = {}
        for item in field_candidates:
            field_id = item.get("field_id", "")
            target = mapping.get(field_id, "ignore")
            if target not in SUPPORTED_TARGETS:
                target = "ignore"
            clean_mapping[field_id] = target
        return clean_mapping

    def classify_interactable_elements(self, elements: List[Dict]) -> Dict[str, str]:
        """Classify scanned interactables, including consent and submit controls."""
        if not elements:
            return {}

        prompt = {
            "instructions": [
                "Classify each interactable element into exactly one target label.",
                "Allowed labels: request_description, email, name, phone, street_address, city, state, zip, company, department, consent_checkbox, submit_button, ignore.",
                "Use consent_checkbox for required acknowledgement/terms checkboxes/radios.",
                "Use submit_button for the action that should submit/create/make the request.",
                "If uncertain, use ignore.",
                "Output STRICT JSON object only: {\"mapping\": {\"<element_id>\": \"<label>\"}}",
            ],
            "elements": elements,
        }

        completion = self.client.chat.completions.create(
            model=self.model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "You classify NextRequest form interactable elements."},
                {"role": "user", "content": json.dumps(prompt)},
            ],
        )

        payload = json.loads(completion.choices[0].message.content or "{}")
        mapping = payload.get("mapping", {})

        clean_mapping: Dict[str, str] = {}
        for item in elements:
            element_id = item.get("element_id", "")
            target = mapping.get(element_id, "ignore")
            if target not in SUPPORTED_TARGETS:
                target = "ignore"
            clean_mapping[element_id] = target

        return clean_mapping

    def choose_request_entry_candidate(self, candidates: List[Dict]) -> Optional[str]:
        """Use OpenAI to pick the best request-entry candidate id from landing-page candidates."""
        if not candidates:
            return None

        prompt = {
            "task": "Pick the best candidate that most likely opens a NEW public records request form on a NextRequest portal.",
            "rules": [
                "Prefer hrefs containing /requests/new.",
                "Prefer visible enabled links/buttons mentioning make request/new request/public records request.",
                "Return ONLY JSON with key selected_candidate_id.",
                "If uncertain, pick the single best candidate anyway.",
            ],
            "candidates": candidates,
        }

        completion = self.client.chat.completions.create(
            model=self.model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": "You are selecting the best landing-page action for opening a NextRequest new request form.",
                },
                {"role": "user", "content": json.dumps(prompt)},
            ],
        )

        payload = json.loads(completion.choices[0].message.content or "{}")
        selected = payload.get("selected_candidate_id")
        if not isinstance(selected, str):
            return None

        valid_ids = {str(c.get("candidate_id", "")) for c in candidates}
        return selected if selected in valid_ids else None

    def choose_department_option(self, options: List[str]) -> Optional[str]:
        """Choose the best department option text for public records routing."""
        if not options:
            return None

        prompt = {
            "task": "Select the best department option for a public records request.",
            "priority": [
                "Controller",
                "Tax",
                "Finance",
                "Clerk",
                "Records",
                "Administration",
            ],
            "rules": [
                "Pick exactly one option from the provided list.",
                "Prefer matches aligned with the priority order.",
                "Return STRICT JSON only: {\"selected_option\": \"<exact option text>\"}",
            ],
            "options": options,
        }

        completion = self.client.chat.completions.create(
            model=self.model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": "You choose the most appropriate department option for NextRequest public records submissions.",
                },
                {"role": "user", "content": json.dumps(prompt)},
            ],
        )

        payload = json.loads(completion.choices[0].message.content or "{}")
        selected = payload.get("selected_option")
        if not isinstance(selected, str):
            return None

        normalized = {o.strip().lower(): o for o in options}
        return normalized.get(selected.strip().lower())
