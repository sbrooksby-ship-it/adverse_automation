import os
import pickle
import re
import time
from datetime import datetime
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload
from playwright.sync_api import sync_playwright

# --- CONFIGURATION & ENV VARS ---
FIVE9_USER = os.environ.get("FIVE9_USER", "div5vcc@bonadmin.com")
FIVE9_PASS = os.environ.get("FIVE9_PASS", "Div5Rules!")
# Updated Google Drive Folder ID
GOOGLE_FOLDER_ID = os.environ.get("GOOGLE_FOLDER_ID", "1H5jrB7bxLLoneHY1GsG3aBsHKmMygvzZ")
HANDLED_FOLDER_ID = "1I4Xfnuvm31-rhEXypFVqFkpqzjR-a5nU"
CLIENT_SECRET_FILE = "client_secret.json"
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def get_drive_service():
    """Authenticates using stored token or initiates OAuth flow."""
    creds = None
    if os.path.exists("token.pickle"):
        with open("token.pickle", "rb") as token:
            creds = pickle.load(token)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                CLIENT_SECRET_FILE, DRIVE_SCOPES
            )
            creds = flow.run_local_server(port=0)

        with open("token.pickle", "wb") as token:
            pickle.dump(creds, token)

    return build("drive", "v3", credentials=creds)


def is_already_in_drive(drive_service, call_id):
    """Checks both the Source and Handled folders to avoid duplicates."""
    query = f"('{GOOGLE_FOLDER_ID}' in parents or '{HANDLED_FOLDER_ID}' in parents) and name contains '{call_id}' and trashed = false"
    results = drive_service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])
    return len(files) > 0


def upload_transcript_to_drive(drive_service, file_name, text_content):
    """Uploads transcript text directly from memory into Google Drive."""
    file_metadata = {
        "name": file_name,
        "parents": [GOOGLE_FOLDER_ID]
    }
    media = MediaInMemoryUpload(text_content.encode("utf-8"), mimetype="text/plain")
    uploaded_file = drive_service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id"
    ).execute()
    print(f"Uploaded '{file_name}' to Drive. (ID: {uploaded_file.get('id')})")


def run_hourly_extraction():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Launching Playwright browser...")
    drive_service = get_drive_service()

    with sync_playwright() as p:
        headless_mode = os.environ.get("HEADLESS_MODE", "true").lower() == "true"
        browser = p.chromium.launch(headless=headless_mode)

        if os.path.exists("state.json"):
            context = browser.new_context(storage_state="state.json", accept_downloads=True)
        else:
            context = browser.new_context(accept_downloads=True)

        page = context.new_page()

        try:
            print("Navigating to Five9 Admin Console...")
            page.goto("https://admin.us.five9.net/", wait_until="networkidle")
            page.wait_for_timeout(3000)

            # --- LOGIN DETECTOR ---
            dashboard_visible = page.get_by_text("AI Insights").first.is_visible()

            if not dashboard_visible:
                print("Dashboard not found. Login required. Entering credentials...")
                page.get_by_test_id("input").first.click()
                page.get_by_test_id("input").first.fill(FIVE9_USER)
                page.get_by_role("button", name="Next").click()
                page.wait_for_timeout(2000)

                password_field = page.get_by_role("textbox", name="Password Password")
                password_field.click()
                password_field.fill(FIVE9_PASS)

                page.get_by_role("button", name="Sign In").click()

                print("Credentials submitted. Waiting for dashboard...")
                page.wait_for_timeout(8000)

                context.storage_state(path="state.json")
                print("Login complete. Saved updated session state.")
            else:
                print("Active session detected! AI Insights is visible.")

            print("Navigating to AI Insights...")
            if page.locator("div").filter(has_text=re.compile(r"^AI InsightsExplore actionable AI-driven analytics$")).first.is_visible():
                page.locator("div").filter(has_text=re.compile(r"^AI InsightsExplore actionable AI-driven analytics$")).first.click(force=True)
            elif page.locator(".HomeCard-icon").first.is_visible():
                page.locator(".HomeCard-icon").first.click(force=True)

            page.goto("https://admin.us.five9.net/ai-insights", wait_until="domcontentloaded")
            page.wait_for_timeout(6000)

            # --- DEFINE IFRAME HIERARCHY ---
            ai_frame = page.frame_locator('iframe[title="AI Insights"]')
            transcripts_frame = ai_frame.frame_locator('#Transcripts')
            grid_frame = transcripts_frame.frame_locator('iframe')

            print("Navigating to Transcripts tab...")
            ai_frame.get_by_role("menuitem", name="Transcripts").first.click()
            page.wait_for_timeout(4000)

            # --- APPLY BASE DATE FILTER ---
            print("Setting date filter to 'Today'...")
            grid_frame.get_by_role("button", name="Last 7 Days").click()
            page.wait_for_timeout(1000)
            grid_frame.get_by_role("menuitem", name="Today").click()
            page.wait_for_timeout(1000)

            # =========================================================
            # --- UPDATED FILTERS FROM CODEGEN ---
            # =========================================================
            print("Applying custom data filters...")

            # Filter 1: Skill Name Checkbox Array
            try:
                grid_frame.get_by_test_id("filter-token").nth(3).click()
                page.wait_for_timeout(800)

                if not grid_frame.locator(".FauxCheckbox-sc-1yuna8r-0 > svg").first.is_visible():
                    if grid_frame.get_by_test_id("filter-token").nth(4).is_visible():
                        grid_frame.get_by_test_id("filter-token").nth(4).click()
                        page.wait_for_timeout(800)

                skill_id_checkbox = grid_frame.locator('[id="-1815748261"] > .Flex-sc-1ak395a-0 > .FauxCheckbox-sc-1yuna8r-0 > svg')
                if skill_id_checkbox.is_visible():
                    skill_id_checkbox.click(force=True)
                else:
                    grid_frame.locator(".FauxCheckbox-sc-1yuna8r-0 > svg").first.click(force=True)

                grid_frame.get_by_role("button", name="Done").click()
                page.wait_for_timeout(1000)
            except Exception as e:
                print(f"Warning: Issue clicking Skill Name filter: {e}")

            # Filter 2: Duration >= 90
            try:
                grid_frame.get_by_role("button", name="is >=").click()
                page.wait_for_timeout(500)
                grid_frame.get_by_test_id("single-number").click()
                grid_frame.get_by_test_id("single-number").fill("90")
                grid_frame.locator("body").click(force=True)
                page.wait_for_timeout(1000)
            except Exception as e:
                print(f"Warning: Issue setting >= 90 filter: {e}")

            # Filter 3: Exclude Call Types / Dispositions ("doesn't contain" + All 12 Dispositions)
            try:
                grid_frame.get_by_role("button", name="is any value").nth(4).click()
                page.wait_for_timeout(1000)

                grid_frame.get_by_role("combobox").nth(1).click()
                page.wait_for_timeout(500)
                if grid_frame.get_by_role("option", name="doesn't contain", exact=True).is_visible():
                    grid_frame.get_by_role("option", name="doesn't contain", exact=True).click()
                else:
                    page.keyboard.type("doesn't contain")
                    page.keyboard.press("Enter")
                page.wait_for_timeout(800)

                if not grid_frame.locator(".FauxCheckbox-sc-1yuna8r-0").first.is_visible():
                    any_val = grid_frame.get_by_label("any value").first
                    if any_val.is_visible():
                        any_val.click()
                    elif grid_frame.get_by_text("any value", exact=True).first.is_visible():
                        grid_frame.get_by_text("any value", exact=True).first.click()
                    page.wait_for_timeout(800)

                grid_frame.locator(".FauxCheckbox-sc-1yuna8r-0 > svg").first.click(force=True)
                grid_frame.locator(".FauxCheckbox-sc-1yuna8r-0.YZEGd > svg").first.click(force=True)
                grid_frame.locator('[id="-1116953908"] > .Flex-sc-1ak395a-0').click(force=True)
                grid_frame.locator('[id="898302833"] > .Flex-sc-1ak395a-0 > .FauxCheckbox-sc-1yuna8r-0 > svg').click(force=True)
                grid_frame.locator('[id="2085989673"] > .Flex-sc-1ak395a-0 > .FauxCheckbox-sc-1yuna8r-0 > svg').click(force=True)
                grid_frame.locator('[id="272370864"] > .Flex-sc-1ak395a-0 > .FauxCheckbox-sc-1yuna8r-0 > svg').click(force=True)
                grid_frame.locator('[id="548446974"] > .Flex-sc-1ak395a-0 > .FauxCheckbox-sc-1yuna8r-0 > svg').click(force=True)
                grid_frame.locator('[id="75779"] > .Flex-sc-1ak395a-0 > .FauxCheckbox-sc-1yuna8r-0').click(force=True)
                grid_frame.locator('[id="-1670000171"] > .Flex-sc-1ak395a-0 > .FauxCheckbox-sc-1yuna8r-0 > svg').click(force=True)
                grid_frame.locator('[id="-1798048617"] > .Flex-sc-1ak395a-0 > .FauxCheckbox-sc-1yuna8r-0 > svg').click(force=True)
                grid_frame.locator('[id="1917852010"] > .Flex-sc-1ak395a-0 > .FauxCheckbox-sc-1yuna8r-0 > svg').click(force=True)
                grid_frame.locator('[id="1614167563"] > .Flex-sc-1ak395a-0 > .FauxCheckbox-sc-1yuna8r-0 > svg').click(force=True)

                grid_frame.get_by_role("button", name="Done").click()
                page.wait_for_timeout(1000)
            except Exception as e:
                print(f"Warning: Issue setting Disposition filters: {e}")
            # =========================================================

            print("Clicking 'Update' refresh button...")
            grid_frame.get_by_role("button", name="Update").click()
            page.wait_for_timeout(6000)

            page.screenshot(path="debug_grid_state.png", full_page=True)
            print("Saved debug_grid_state.png - Check artifacts to see the filtered table.")

            # --- ITERATIVE SCROLL AND PROCESS LOOP ---
            print("Processing virtualized grid items...")
            processed_call_ids = set()
            consecutive_empty_scrolls = 0

            while consecutive_empty_scrolls < 8:
                visible_ids = grid_frame.locator("button, a, [role='button']").evaluate_all(r"""
                    (elements) => elements
                        .map(el => el.innerText.trim())
                        .map(text => {
                            let match = text.match(/\b(\d{7})\b/);
                            return match ? match[1] : null;
                        })
                        .filter(id => id !== null)
                """)

                unprocessed_ids = [cid for cid in visible_ids if cid not in processed_call_ids]

                if unprocessed_ids:
                    call_id = unprocessed_ids[0]
                    processed_call_ids.add(call_id)
                    consecutive_empty_scrolls = 0
