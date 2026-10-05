import os
import pickle
import re
from datetime import datetime
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload
from playwright.sync_api import sync_playwright

# --- CONFIGURATION & ENV VARS ---
FIVE9_USER = os.environ.get("FIVE9_USER")
FIVE9_PASS = os.environ.get("FIVE9_PASS")

# Your exact requested Google Drive Folder ID
GOOGLE_FOLDER_ID = "10fCNy7z2nqxbIzGFwP7cRrYQm6PK--zp"
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
    """Checks the target folder to avoid downloading duplicates."""
    query = f"'{GOOGLE_FOLDER_ID}' in parents and name contains '{call_id}' and trashed = false"
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
        # Defaults to headed mode so you can watch it unless HEADLESS_MODE=true is set
        headless_mode = os.environ.get("HEADLESS_MODE", "false").lower() == "true"
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
                page.get_by_test_id("input").click()
                page.get_by_test_id("input").fill(FIVE9_USER)
                page.get_by_role("button", name="Next").click()
                page.wait_for_timeout(2000)

                password_field = page.get_by_role("textbox", name=re.compile("Password"))
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
            if page.locator(".HomeCard-icon").first.is_visible():
                page.locator(".HomeCard-icon").first.click(force=True)

            page.goto("https://admin.us.five9.net/ai-insights", wait_until="domcontentloaded")
            page.wait_for_timeout(6000)

            # --- DEFINE IFRAME HIERARCHY ---
            ai_frame = page.frame_locator('iframe[title="AI Insights"]')
            transcripts_frame = ai_frame.frame_locator('#Transcripts')
            grid_frame = transcripts_frame.frame_locator('iframe')

            print("Navigating to Transcripts tab...")
            ai_frame.get_by_text("Transcripts").first.click()
            page.wait_for_timeout(5000)

            # =====================================================================
            # --- APPLY YOUR CUSTOM CODEGEN FILTERS WITH EXPLICIT WAITS ---
            # =====================================================================
            print("Applying custom filters from codegen script...")
            
            # 1. Custom Token setup
            grid_frame.get_by_test_id('filter-token').nth(3).click()
            page.wait_for_timeout(1000)
            grid_frame.locator('[id="-82805172"] > .Flex-sc-1ak395a-0 > .FauxCheckbox-sc-1yuna8r-0 > svg').click()
            grid_frame.locator('[id="-569451958"] > .Flex-sc-1ak395a-0 > .FauxCheckbox-sc-1yuna8r-0 > svg').click()
            grid_frame.get_by_role('button', name='Done').click()
            page.wait_for_timeout(1000)

            # 2. Date Filter ("Last 7 Days" -> "Today")
            grid_frame.get_by_role('button', name='Last 7 Days').click()
            page.wait_for_timeout(500)
            grid_frame.get_by_role('menuitem', name='Today').click()
            page.wait_for_timeout(500)
            grid_frame.get_by_role('button', name='Update').click()
            page.wait_for_timeout(3000)

            # 3. Add "is not blank" filter block
            grid_frame.get_by_role('button', name='is any value').nth(4).click()
            page.wait_for_timeout(500)
            grid_frame.get_by_role('combobox').nth(1).click()
            page.wait_for_timeout(500)
            grid_frame.get_by_role('dialog', name=re.compile('is not blank')).get_by_role('combobox').click()
            grid_frame.get_by_role('dialog', name=re.compile("doesn't contain")).get_by_placeholder('any value').click()
            page.wait_for_timeout(500)
            grid_frame.locator('[id="898302833"] > .Flex-sc-1ak395a-0 > .FauxCheckbox-sc-1yuna8r-0 > svg').click()
            grid_frame.locator('[id="548446974"] > .Flex-sc-1ak395a-0 > .FauxCheckbox-sc-1yuna8r-0 > svg').click()
            grid_frame.get_by_role('button', name='Done').click()
            grid_frame.get_by_role('button', name='Update').click()
            page.wait_for_timeout(3000)

            # 4. Add "does not contain Call Drop" filter block
            grid_frame.get_by_role('button', name=re.compile('does not contain Call Drop')).click()
            page.wait_for_timeout(500)
            grid_frame.get_by_role('dialog', name=re.compile("doesn't contain")).get_by_label('any value').click()
            grid_frame.locator('[id="-688236928"] > .Flex-sc-1ak395a-0 > .FauxCheckbox-sc-1yuna8r-0 > svg').click()
            grid_frame.get_by_role('button', name='Done').click()
            grid_frame.get_by_role('button', name='Update').click()
            
            print("Filters applied! Waiting for grid to settle...")
            page.wait_for_timeout(6000)
            # =====================================================================

            # --- ITERATIVE SCROLL AND PROCESS LOOP ---
            print("Processing virtualized grid items...")
            processed_call_ids = set()
            consecutive_empty_scrolls = 0

            # Scroll loop - allows it to pull calls all the way down the page
            while consecutive_empty_scrolls < 8:

                # Scrapes all visible 7-digit Call IDs from the current grid view
                visible_ids = grid_frame.locator("button, a, [role='gridcell'], [role='button'], .ag-cell").evaluate_all(r"""
                    (elements) => elements
                        .map(el => el.innerText.trim())
                        .map(text => {
                            let match = text.match(/\b(\d{7})\b/);
                            return match ? match[1] : null;
                        })
                        .filter(id => id !== null)
                """)

                # Find calls we haven't touched in this session yet
                unprocessed_ids = [cid for cid in visible_ids if cid not in processed_call_ids]

                if unprocessed_ids:
                    # Process exactly ONE call, then let the loop restart to re-evaluate the screen
                    call_id = unprocessed_ids[0]
                    processed_call_ids.add(call_id)
                    consecutive_empty_scrolls = 0  # Reset scroll counter

                    print(f"\n--- Processing Call ID: {call_id} ---")

                    # SKIP CHECK: Verify with Google Drive first
                    if is_already_in_drive(drive_service, call_id):
                        print(f"Skipping Call ID {call_id} (Already exists in Google Drive).")
                        continue

                    try:
                        # Locate the specific button for this ID dynamically
                        btn = grid_frame.get_by_role("button", name=re.compile(call_id)).first
                        btn.scroll_into_view_if_needed()
                        btn.click(force=True)
                        page.wait_for_timeout(1500)

                        grid_frame.get_by_role("menuitem", name=re.compile("View Transcript")).click()
                        page.wait_for_timeout(3000)

                        transcripts_frame.get_by_test_id("Dropdown").get_by_role("button", name="Transcript").click()
                        page.wait_for_timeout(1000)

                        # Trigger the download intercept
                        with page.expect_download(timeout=15000) as download_info:
                            transcripts_frame.get_by_role("menuitem", name="Download Transcript").click()

                        download = download_info.value
                        temp_filepath = os.path.join(os.getcwd(), download.suggested_filename)
                        download.save_as(temp_filepath)

                        with open(temp_filepath, "r", encoding="utf-8") as f:
                            file_content = f.read()

                        # PUSH TO GOOGLE DRIVE
                        upload_transcript_to_drive(drive_service, download.suggested_filename, file_content)
                        os.remove(temp_filepath)

                        # SAFELY CLOSE MODAL AVOIDING STRICT MODE VIOLATIONS
                        close_btn = transcripts_frame.get_by_role("button", name="Close")
                        cancel_btn = transcripts_frame.get_by_role("button", name="Cancel")

                        if close_btn.count() > 0 and close_btn.first.is_visible():
                            close_btn.first.click()
                        elif cancel_btn.count() > 0 and cancel_btn.first.is_visible():
                            cancel_btn.first.click()
                        else:
                            page.keyboard.press("Escape")

                        page.wait_for_timeout(1500)

                    except Exception as ex:
                        print(f"Failed to process Call ID {call_id}. Error: {ex}")
                        if not page.is_closed():
                            page.keyboard.press("Escape")
                            page.wait_for_timeout(1500)

                else:
                    # Everything currently on screen is processed. We must scroll down.
                    print("No new calls visible. Scrolling down to load more...")

                    # Force focus onto the body of the specific iframe before scrolling
                    grid_frame.locator("body").click(force=True)
                    page.mouse.wheel(delta_x=0, delta_y=600)

                    consecutive_empty_scrolls += 1
                    
                    # Wait 3 seconds to let the next batch of rows render
                    page.wait_for_timeout(3000)

            print(f"\nSUCCESS! Completed extraction. Processed {len(processed_call_ids)} total calls.")

        except Exception as e:
            print(f"Navigation error: {e}")
            if not page.is_closed():
                page.screenshot(path="error_screenshot.png")

        finally:
            browser.close()


if __name__ == "__main__":
    run_hourly_extraction()
