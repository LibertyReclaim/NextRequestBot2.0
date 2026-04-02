"""Entrypoint for NextRequest request-submission automation."""

from __future__ import annotations

from playwright.sync_api import sync_playwright

from ai_field_mapper import AIFieldMapper
from config import INPUT_XLSX, get_openai_api_key, get_requester_profile
from excel_logger import ExcelLogger
from request_submitter import RequestSubmitter


def main() -> None:
    profile = get_requester_profile()
    ai_mapper = AIFieldMapper(api_key=get_openai_api_key())
    logger = ExcelLogger(str(INPUT_XLSX))

    rows = logger.iter_rows()
    print("Starting bot...")
    print(f"Loaded {len(rows)} rows")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        submitter = RequestSubmitter(page=page, profile=profile, ai_mapper=ai_mapper)

        for i, row in enumerate(rows, start=1):
            print(f"Processing row {i} - {row.municipality}")
            result = submitter.submit(municipality=row.municipality, portal_url=row.portal_url)
            logger.log_result(row.index, result)
            logger.save()
            print("Saved result")

        context.close()
        browser.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ERROR:", e)
        import traceback

        traceback.print_exc()
