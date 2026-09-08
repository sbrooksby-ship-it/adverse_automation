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
FIVE9_USER = os.environ.get("FIVE9_USER")
FIVE9_PASS = os.environ.get("FIVE9_PASS")
GOOGLE_FOLDER_ID = os.environ.get("GOOGLE_FOLDER_ID", "1OC6DngtZwWse5o9DTqI8P2sIiSddn5vv")
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
            page.wait_for_timeout(5000) 

            # --- LOGIN DETECTOR ---
            dashboard_visible = page.get_by_text("AI Insights").first.is_visible()

            if not dashboard_visible:
                print("Dashboard not found. Login required. Entering credentials...")
                page.get_by_test_id("input").click()
                page.get_by_test_id("input").fill(FIVE9_USER)
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

            print("Clicking AI Insights tile...")
            page.locator("div").filter(has_text=re.compile(r"^AI InsightsExplore actionable AI-driven analytics$")).first.click()
            page.wait_for_timeout(6000)

            # --- DEFINE IFRAME HIERARCHY ---
            ai_frame = page.frame_locator('iframe[title="AI Insights"]')
            transcripts_frame = ai_frame.frame_locator('#Transcripts')
            grid_frame = transcripts_frame.frame_locator('iframe')

            print("Navigating to Transcripts tab...")
            ai_frame.get_by_text("Transcripts").click()
            page.wait_for_timeout(5000)

            # --- APPLY RELIABLE FILTERS ---
            print("Applying filter combinations...")
            grid_frame.get_by_test_id("filter-token").nth(2).click()
            grid_frame.get_by_test_id("surface-content").get_by_role("combobox", name="any value").click()
            grid_frame.get_by_role("dialog", name="contains contains Add").get_by_placeholder("any value").fill("Customer Sale")
            grid_frame.get_by_role("button", name="Done").click()

            grid_frame.get_by_role("button", name="Last 7 Days").click()
            grid_frame.get_by_role("menuitem", name="Today").click()

            grid_frame.get_by_role("button", name="contains Customer Sale").click()
            grid_frame.locator("div").filter(has_text=re.compile(r"^Customer SaleDelete$")).click()
            grid_frame.get_by_role("dialog", name="contains contains Customer").get_by_label("any value").fill("New Sales")
            grid_frame.get_by_role("button", name="Done").click()

            print("Clicking 'Update' refresh button...")
            grid_frame.get_by_role("button", name="Update").click()
            page.wait_for_timeout(6000)

            # --- ITERATIVE SCROLL AND PROCESS LOOP ---
            print("Processing virtualized grid items...")
            processed_call_ids = set()
            consecutive_empty_scrolls = 0

            while consecutive_empty_scrolls < 4:
                # Find all currently visible call buttons (matching 6 to 12 digit IDs)
                visible_buttons = grid_frame.get_by_role("button", name=re.compile(r"^\d{6,12}$")).all()
                found_new_in_pass = False

                for btn in visible_buttons:
                    try:
                        call_id = btn.inner_text().strip()
                        if call_id in processed_call_ids:
                            continue

                        processed_call_ids.add(call_id)
                        found_new_in_pass = True
                        print(f"\n--- Processing Call ID: {call_id} ---")

                        if is_already_in_drive(drive_service, call_id):
                            print(f"Skipping Call ID {call_id} (Already exists in Google Drive).")
                            continue

                        # Ensure element is visible and click
                        btn.scroll_into_view_if_needed()
                        btn.click(force=True)
                        page.wait_for_timeout(1500)

                        # Open transcript view
                        grid_frame.get_by_role("menuitem", name=re.compile("View Transcript")).click()
                        page.wait_for_timeout(3000)

                        # Open menu dropdown & download
                        transcripts_frame.get_by_test_id("Dropdown").get_by_role("button", name="Transcript").click()
                        page.wait_for_timeout(1000)

                        with page.expect_download(timeout=15000) as download_info:
                            transcripts_frame.get_by_role("menuitem", name="Download Transcript").click()

                        download = download_info.value
                        temp_filepath = os.path.join(os.getcwd(), download.suggested_filename)
                        download.save_as(temp_filepath)

                        with open(temp_filepath, "r", encoding="utf-8") as f:
                            file_content = f.read()

                        upload_transcript_to_drive(drive_service, download.suggested_filename, file_content)
                        os.remove(temp_filepath)

                        # Close Modal
                        transcripts_frame.get_by_role("button", name="Close").click()
                        page.wait_for_timeout(1500)

                    except Exception as ex:
                        print(f"Failed to process Call ID {call_id}. Error: {ex}")
                        if not page.is_closed():
                            page.keyboard.press("Escape")
                            page.wait_for_timeout(1500)

                # Scroll the grid viewport down by 400px to bring the next batch into DOM
                can_scroll_further = grid_frame.locator("body").evaluate("""
                    () => {
                        let container = document.querySelector('div[role="grid"], .ag-body-viewport, .MuiDataGrid-virtualScroller, div[class*="scroll"]') || document.body;
                        let startPos = container.scrollTop;
                        container.scrollBy(0, 400);
                        return container.scrollTop > startPos;
                    }
                """)

                if not found_new_in_pass and not can_scroll_further:
                    consecutive_empty_scrolls += 1
                elif found_new_in_pass:
                    consecutive_empty_scrolls = 0

                page.wait_for_timeout(1500)

            print(f"\nSUCCESS! Completed extraction. Processed {len(processed_call_ids)} total calls.")

        except Exception as e:
            print(f"Navigation error: {e}")
            if not page.is_closed():
                page.screenshot(path="error_screenshot.png")

        finally:
            browser.close()


if __name__ == "__main__":
    run_hourly_extraction()
