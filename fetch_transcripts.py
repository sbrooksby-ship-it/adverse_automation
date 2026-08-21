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
            context = browser.new_context(storage_state="state.json")
        else:
            context = browser.new_context()
            
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

                page.get_by_role("textbox", name="Password Password").click()
                page.get_by_role("textbox", name="Password Password").fill(FIVE9_PASS)
                page.get_by_role("button", name="Sign In").click()
                
                print("Credentials submitted. Waiting for dashboard...")
                page.wait_for_timeout(8000)
                
                context.storage_state(path="state.json")
                print("Login complete. Saved updated session state.")
            else:
                print("Active session detected! AI Insights is visible.")

            print("Clicking AI Insights tile...")
            page.get_by_text("AI Insights").first.click()
            page.wait_for_timeout(6000)

            # --- DEFINE IFRAME HIERARCHY ---
            ai_frame = page.frame_locator('iframe[title="AI Insights"]')
            transcripts_frame = ai_frame.frame_locator('#Transcripts')
            grid_frame = transcripts_frame.frame_locator('iframe')

            print("Navigating to Transcripts tab...")
            ai_frame.get_by_role("menuitem", name="Transcripts").click()
            page.wait_for_timeout(8000)

            # --- APPLY FILTER: TODAY & UPDATE ---
            print("Setting date filter to 'Today'...")
            grid_frame.get_by_role("button", name="Last 7 Days").click()
            page.wait_for_timeout(1000)
            
            grid_frame.get_by_role("menuitem", name="Today").click()
            page.wait_for_timeout(1000)
            
            print("Clicking 'Update' refresh button...")
            grid_frame.get_by_role("button", name="Update").click()
            page.wait_for_timeout(8000)

            # --- EXTRACT ALL CALL IDs (SCROLL & COLLECT) ---
            print("Scrolling grid to extract all virtualized Call IDs...")
            js_scroll_and_extract = r"""
            async () => {
                let container = document.querySelector('div[role="grid"], .ag-body-viewport, .MuiDataGrid-virtualScroller, div[class*="scroll"]') || document.body;
                let foundIds = new Set();
                let previousSize = -1;
                let unchangedRounds = 0;

                while (unchangedRounds < 4) {
                    let elements = Array.from(document.querySelectorAll('*'));
                    for (let el of elements) {
                        if (el.children.length === 0) {
                            let txt = el.innerText ? el.innerText.trim() : (el.textContent ? el.textContent.trim() : '');
                            if (txt.match(/^\d{7}$/)) {
                                foundIds.add(txt);
                            }
                        }
                    }

                    if (foundIds.size === previousSize) {
                        unchangedRounds++;
                    } else {
                        unchangedRounds = 0;
                        previousSize = foundIds.size;
                    }

                    container.scrollBy(0, 600);
                    await new Promise(resolve => setTimeout(resolve, 600));
                }

                container.scrollTo(0, 0);
                return Array.from(foundIds);
            }
            """
            
            found_call_ids = grid_frame.locator("body").evaluate(js_scroll_and_extract)
            print(f"\nSUCCESS! Found {len(found_call_ids)} total Call IDs for Today.")

            # --- PROCESS CALL IDs ---
            for call_id in found_call_ids:
                print(f"\n--- Processing Call ID: {call_id} ---")

                # Deduplication Check
                if is_already_in_drive(drive_service, call_id):
                    print(f"Skipping Call ID {call_id} (Already exists in Google Drive).")
                    continue
                
                try:
                    # 1. Scroll & Click Call ID Button
                    call_btn = grid_frame.get_by_role("button", name=call_id).first
                    call_btn.scroll_into_view_if_needed()
                    call_btn.click(force=True)
                    page.wait_for_timeout(1500)
                    
                    # 2. Click 'Explore View Transcript'
                    grid_frame.get_by_role("menuitem", name=re.compile("View Transcript")).click()
                    print("Opened transcript modal...")
                    page.wait_for_timeout(4000) 
                    
                    # 3. Click 3-Dots dropdown
                    transcripts_frame.get_by_test_id("Dropdown").get_by_role("button", name="Transcript").click()
                    page.wait_for_timeout(1500)

                    # 4. Intercept Download
                    print("Intercepting transcript download...")
                    with page.expect_download(timeout=15000) as download_info:
                        transcripts_frame.get_by_role("menuitem", name="Download Transcript").click()
                    
                    download = download_info.value
                    temp_filepath = os.path.join(os.getcwd(), download.suggested_filename)
                    download.save_as(temp_filepath)
                    
                    with open(temp_filepath, "r", encoding="utf-8") as f:
                        file_content = f.read()
                        
                    # 5. Upload & Cleanup Local Copy
                    upload_transcript_to_drive(drive_service, download.suggested_filename, file_content)
                    os.remove(temp_filepath)
                    
                    # 6. Close Modal
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(2000)
                    
                except Exception as ex:
                    print(f"Failed to process Call ID {call_id}. Error: {ex}")
                    if not page.is_closed():
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(2000)

        except Exception as e:
            print(f"Navigation error: {e}")
            if not page.is_closed():
                page.screenshot(path="error_screenshot.png")

        finally:
            browser.close()


if __name__ == "__main__":
    run_hourly_extraction()
