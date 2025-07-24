import os
import sys
import json
import re
import pickle
import threading
import difflib
import requests
from datetime import datetime, timezone
import weakref

from PyQt6.QtWidgets import *
from PyQt6.QtCore import *
from PyQt6.QtGui import *
import os


from dotenv import load_dotenv
load_dotenv()
import google.generativeai as genai
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import traceback
import faulthandler
faulthandler.enable()          # Dump tracebacks on fatal errors





def excepthook(exc_type, exc_value, exc_tb):

    lines = traceback.format_exception(exc_type, exc_value, exc_tb)

    print("".join(lines))
    sys.exit(1)
sys.excepthook = excepthook
# ------------------------------------------------------------------
# 1.  Constants & Globals
# ------------------------------------------------------------------
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_FILE = "gmail_token.pickle"
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_KEY)
GEMINI_MODEL = genai.GenerativeModel("gemini-1.5-flash")

# ------------------------------------------------------------------
# 2.  Gmail Monitor (no-n8n)
# ------------------------------------------------------------------
class GmailMonitor(QObject):
    result_found = pyqtSignal(str, str)

    def __init__(self, apps, interval=15):
        super().__init__()
        self.apps = apps
        self.interval = interval
        self.timer = QTimer()
        self.timer.setInterval(interval * 60 * 1000)
        self.timer.timeout.connect(self._tick)
        self.service = None

    def _get_creds(self):
        if not os.path.exists(TOKEN_FILE):
            return None
        creds = pickle.load(open(TOKEN_FILE, "rb"))
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            pickle.dump(creds, open(TOKEN_FILE, "wb"))
        return creds

    def start(self):
        creds = self._get_creds()
        if not creds:
            return
        self.service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self.timer.start()
        self._tick()

    def stop(self):
        self.timer.stop()

    def reload_apps(self, apps):
        self.apps = apps

    def _tick(self):
        threading.Thread(target=self._poll, daemon=True).start()

    def _poll(self):
        if not self.service:
            return
        for app in self.apps:
            if not app.get("auto_monitor"):
                continue
            domains = app.get("school_domains", [])
            if not domains:
                continue
            q = self._query(domains)
            msgs = self._search(q)
            for m in msgs:
                txt = self._body(m["id"])
                res = self._decide(txt)
                if res:
                    self.result_found.emit(app["id"], res)
                    self._mark_read(m["id"])

    # ---- Gmail helpers ------------------------------------------------
    def _query(self, domains):
        d = " OR ".join(f"from:@{dom}" for dom in domains)
        kw = ("decision OR admitted OR rejected OR waitlist OR deferred OR "
              "congrat OR denied OR acceptance OR decision")
        return f"({d}) ({kw}) is:unread"

    def _search(self, query):
        resp = self.service.users().messages().list(
            userId="me", q=query, maxResults=5
        ).execute()
        return resp.get("messages", [])

    def _body(self, msg_id):
        msg = self.service.users().messages().get(
            userId="me", id=msg_id, format="full"
        ).execute()
        parts = msg["payload"].get("parts", [])
        text = ""
        for p in parts:
            if p.get("mimeType") == "text/plain":
                import base64
                data = p["body"]["data"]
                text += base64.urlsafe_b64decode(data).decode("utf-8", "ignore")
        return text

    def _decide(self, text):
        prompt = ("Return exactly one word: Accepted / Rejected / Waitlisted / Deferred, "
                  "or None if no clear decision.\n\nEmail:\n" + text)
        try:
            ans = GEMINI_MODEL.generate_content(prompt).text.strip()
            return ans if ans in {"Accepted", "Rejected", "Waitlisted", "Deferred"} else None
        except Exception as e:
            print("Gemini decide error:", e)
            return None

    def _mark_read(self, msg_id):
        self.service.users().messages().modify(
            userId="me", id=msg_id, body={"removeLabelIds": ["UNREAD"]}
        ).execute()


# --- Helper Functions (College Scorecard API interaction) ---

def fuzzy_match_titles(user_input, all_titles, cutoff=0.6):
    """
    Returns a list of fuzzy-matched major titles from a list of available titles.
    """
    return difflib.get_close_matches(user_input.lower(), [title.lower() for title in all_titles], n=10, cutoff=cutoff)


def fuzzy_match_major(major_input, available_titles, cutoff=0.6):
    matches = difflib.get_close_matches(major_input.lower(), [t.lower() for t in available_titles], n=3, cutoff=cutoff)
    return matches


def fetch_colleges(min_sat=0, max_sat=1600, state="", ownership="", api_key="fIrC5AldgOegvmMhUPhiaV0N7rYu31QkV3pagMsc"):
    url = "https://api.data.gov/ed/collegescorecard/v1/schools.json"
    fields = [
        "school.name",
        "school.city",
        "school.state",
        "school.school_url",
        "latest.admissions.admission_rate.overall",
        "latest.admissions.sat_scores.average.overall",
        "latest.student.size"
    ]

    params = {
        "api_key": api_key,
        "fields": ",".join(fields),
        "per_page": 100,
        "latest.admissions.sat_scores.average.overall__range": f"{min_sat}..{max_sat}"
    }

    if state:
        params["school.state"] = state

    if ownership:
        params["school.ownership"] = ownership

    try:
        headers = {
            "User-Agent": "PathwiseApp/1.0 (Contact: ishraq.iqbal@example.com)"
        }

        response = requests.get(url, params=params, headers=headers)
        response.raise_for_status()
        print("Requesting:", response.url)
        results = response.json().get("results", [])

        return results

    except requests.exceptions.HTTPError as e:
        print(f"HTTP Error from College Scorecard API: {e.response.status_code} - {e.response.text}")
        error_msg = f"College Scorecard API returned an error ({e.response.status_code})."
        if e.response.status_code == 401:
            error_msg += " Your API key might be invalid or unauthorized."
        elif e.response.status_code == 400:
            error_msg += " The request was invalid. Check your parameters (e.g., SAT range, state codes). Response content suggests an issue with the query itself."
        elif e.response.status_code == 429:
            error_msg += " You might have exceeded the API rate limit (1,000 requests/hour/IP)."
        elif e.response.status_code == 403:
            error_msg += " Access Forbidden. Your API key might be invalid for this type of request or from this origin."
        else:
            error_msg += " Check the console for more details."
        QMessageBox.critical(None, "API Error", error_msg)
        return []
    except requests.exceptions.ConnectionError as e:
        print(f"Connection Error to College Scorecard API: {e}")
        QMessageBox.critical(None, "Network Error",
                             "Could not connect to the College Scorecard API. "
                             "Please check your internet connection.")
        return []
    except requests.exceptions.Timeout as e:
        print(f"Timeout Error from College Scorecard API: {e}")
        QMessageBox.critical(None, "Network Timeout",
                             "College Scorecard API request timed out. "
                             "The server might be busy or your connection is slow.")
        return []
    except Exception as e:
        print(f"An unexpected error occurred while fetching colleges: {e}")
        QMessageBox.critical(None, "Unexpected Error",
                             f"An unexpected error occurred: {e}. "
                             "Please try again or contact support.")
        return []


# Securely load API key for Gemini
gemini_api_key = "AIzaSyD-OVmWRATEH5e18Y06c192Cbn0igs3e3E"
if gemini_api_key:
    genai.configure(api_key=gemini_api_key)
    model = genai.GenerativeModel("models/gemini-1.5-flash")
else:
    print("WARNING: GEMINI_API_KEY environment variable not set. AI features might not work.")
    # You might want to disable AI features or show a persistent warning in the UI
    model = None  # Set model to None if API key is missing


# --- Supporting UI Components (SlideCard, CardHeader, ExpandedCard) ---

class SlideCard(QWidget):
    clicked = pyqtSignal()

    def __init__(self, title, summary, full_text):
        super().__init__()
        self.title = title
        self.summary = summary
        self.full_text = full_text
        self.expanded = False

        self.setObjectName("SlideCard")
        self.setStyleSheet("""
            QWidget#SlideCard {
                background-color: rgba(255, 255, 255, 0.05);
                border-radius: 12px;
                border: 1px solid rgba(255, 255, 255, 0.1);
            }
        """)
        self.setMinimumHeight(100)
        self.setMaximumHeight(100)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(8)

        title_label = QLabel(f"<b>• {title}</b>")
        title_label.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        title_label.setStyleSheet("color: white;")

        self.summary_label = QLabel(summary)
        self.summary_label.setStyleSheet("color: #ccc;")
        self.summary_label.setWordWrap(True)

        self.detail_label = QLabel(full_text)
        self.detail_label.setStyleSheet("color: white;")
        self.detail_label.setWordWrap(True)
        self.detail_label.setVisible(True)
        self.detail_label.setMaximumHeight(0)

        layout.addWidget(title_label)
        layout.addWidget(self.summary_label)
        layout.addWidget(self.detail_label)

        self.anim = QPropertyAnimation(self.detail_label, b"maximumHeight")
        self.anim.setDuration(400)
        self.anim.setEasingCurve(QEasingCurve(QEasingCurve.Type.InOutQuad))

        self.shadow = QGraphicsDropShadowEffect(self)
        self.shadow.setBlurRadius(0)
        self.shadow.setOffset(0, 0)
        self.shadow.setColor(QColor(0, 0, 0, 180))
        self.setGraphicsEffect(self.shadow)

    def mousePressEvent(self, event):
        self.clicked.emit()

    def enterEvent(self, event):
        self.shadow.setBlurRadius(20)
        self.shadow.setOffset(0, 4)

    def leaveEvent(self, event):
        self.shadow.setBlurRadius(0)
        self.shadow.setOffset(0, 0)

    def toggle(self, expand: bool):
        self.expanded = expand
        self.anim.stop()
        if expand:
            self.summary_label.setVisible(False)
            self.anim.setStartValue(self.detail_label.maximumHeight())
            self.anim.setEndValue(self.detail_label.sizeHint().height() + 10)
            self.anim.start()
        else:
            self.summary_label.setVisible(True)
            self.anim.setStartValue(self.detail_label.maximumHeight())
            self.anim.setEndValue(0)
            self.anim.start()


class CardHeader(QWidget):
    clicked = pyqtSignal()

    def __init__(self, title, summary):
        super().__init__()
        layout = QVBoxLayout(self)
        label = QLabel(f"<b>{title}</b>")
        label.setStyleSheet("color: white;")
        summary_label = QLabel(summary)
        summary_label.setStyleSheet("color: #ccc;")
        layout.addWidget(label)
        self.setFixedSize(280, 80)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("background: rgba(255,255,255,0.08); border-radius: 8px;")

    def mousePressEvent(self, event):
        self.clicked.emit()


class ExpandedCard(QWidget):
    def __init__(self, title, content, font=None):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        label = QLabel(title)
        label.setStyleSheet("color: white; font-size: 14px;")
        label.setWordWrap(True)
        label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        if font:
            label.setFont(font)
        layout.addWidget(label, alignment=Qt.AlignmentFlag.AlignTop)

        content_label = QLabel(content)
        content_label.setStyleSheet("color: white;")
        content_label.setWordWrap(True)
        content_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        if font:
            content_label.setFont(font)
        layout.addWidget(content_label)

        self.setStyleSheet("background: rgba(255,255,255,0.12); border-radius: 12px;")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumWidth(300)
        self.title = title


# --- GmailAuthPanel Class ---

# ------------------------------------------------------------------
# 1.  NEW  –  GmailAuthPanel (no-n8n version)
# ------------------------------------------------------------------
import os
import pickle
import threading
from datetime import datetime

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from PyQt6.QtCore import pyqtSignal, QThread, QObject
from PyQt6.QtWidgets import QMessageBox, QDialog, QVBoxLayout, QLabel, QPushButton


SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_FILE = "gmail_token.pickle"


class GmailWorker(QObject):
    """Background worker that performs the OAuth flow and returns success / failure."""
    success = pyqtSignal()
    error   = pyqtSignal(str)

    def run(self):
        creds = None
        if os.path.exists(TOKEN_FILE):
            with open(TOKEN_FILE, "rb") as token:
                creds = pickle.load(token)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as e:
                    self.error.emit(str(e))
                    return
                    # ... inside the run method
                else:
                    # ---- first-time flow ----
                    # Get the path from the environment variable you set in .env
                    credentials_path = os.getenv("GMAIL_CREDENTIALS_PATH")
                    if not credentials_path or not os.path.exists(credentials_path):
                        # This provides a clear error if the file is missing or the .env is wrong
                        self.error.emit(
                            f"Gmail credentials not found at path: {credentials_path}. Check your .env file.")
                        return

                    flow = InstalledAppFlow.from_client_secrets_file(
                        credentials_path, SCOPES
                    )

                try:
                    creds = flow.run_local_server(port=0)
                except Exception as e:
                    self.error.emit(str(e))
                    return

            with open(TOKEN_FILE, "wb") as token:
                pickle.dump(creds, token)

        # Test that we can actually hit the API
        try:
            service = build("gmail", "v1", credentials=creds, cache_discovery=False)
            service.users().getProfile(userId="me").execute()
            self.success.emit()
        except Exception as e:
            self.error.emit(str(e))


class GmailAuthPanel(QDialog):
    auth_successful = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connect Gmail for Application Monitoring")
        self.setFixedSize(500, 300)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)

        self.init_ui()
        self.apply_styles()

    # ------------------------------------------------------------------
    # UI boiler-plate (same look as before)
    # ------------------------------------------------------------------
    def init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(15)

        title = QLabel("Unlock Auto-Monitoring with Gmail")
        title.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        main_layout.addWidget(title)

        desc = QLabel(
            "To automatically track college decisions, we need read-only access to your Gmail inbox. "
            "We only scan for emails from universities you've applied to.\n\n"
            "This uses a secure Google OAuth process. Your password is never stored."
        )
        desc.setFont(QFont("Segoe UI", 12))
        desc.setWordWrap(True)
        desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        main_layout.addWidget(desc)

        self.auth_button = QPushButton("Connect Gmail Account")
        self.auth_button.setFont(QFont("Segoe UI", 13, QFont.Weight.Bold))
        self.auth_button.clicked.connect(self._start_auth)
        main_layout.addWidget(self.auth_button)

        self.status_label = QLabel("Click 'Connect Gmail Account' to begin.")
        self.status_label.setFont(QFont("Segoe UI", 11))
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        main_layout.addWidget(self.status_label)

    def apply_styles(self):
        self.setStyleSheet("""
            QDialog {
                background-color: #1E1E1E;
                border-radius: 10px;
                border: 1px solid #333333;
            }
            QLabel {
                color: #E0E0E0;
                font-family: 'Segoe UI';
            }
            QPushButton {
                background-color: #4285F4;
                color: white;
                padding: 12px 25px;
                border-radius: 8px;
                border: none;
            }
            QPushButton:hover {
                background-color: #357ae8;
            }
            QPushButton:pressed {
                background-color: #2a65cc;
            }
        """)

    # ------------------------------------------------------------------
    # OAuth flow – runs in background thread so UI is not frozen
    # ------------------------------------------------------------------
    def _start_auth(self):
        if not os.path.exists("credentials.json"):
            QMessageBox.critical(
                self,
                "Missing credentials.json",
                "Please download your OAuth 2.0 credentials from Google Cloud Console "
                "and place the file named credentials.json next to this program."
            )
            return

        self.auth_button.setEnabled(False)
        self.status_label.setText("Opening browser…")

        self.worker_thread = QThread()
        self.worker = GmailWorker()
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.worker.run)
        self.worker.success.connect(self._on_success)
        self.worker.error.connect(self._on_error)
        self.worker.success.connect(self.worker_thread.quit)
        self.worker.error.connect(self.worker_thread.quit)
        self.worker_thread.start()

    def _on_success(self):
        self.status_label.setText("Gmail connected successfully!")
        self.accept()
        self.auth_successful.emit()

    def _on_error(self, msg):
        self.status_label.setText("Connection failed.")
        self.auth_button.setEnabled(True)
        QMessageBox.critical(self, "Gmail Connection Error", msg)

# --- ApplicationEntryPanel Class ---

class ApplicationEntryPanel(QWidget):
    app_saved = pyqtSignal(dict)
    gmail_connected = pyqtSignal(bool)

    # --- In the ApplicationEntryPanel class ---

    # ADD THIS ENTIRE METHOD
    def _handle_gmail_auth_success(self, app_data):
        """
        This slot is called after the GmailAuthPanel reports a successful connection.
        It finalizes the application saving process.
        """
        print("Gmail authentication successful. Proceeding to save application.")

        # Set the flag so we don't have to ask again in this session
        self.is_gmail_connected = True
        self.gmail_connected.emit(True)  # Inform the main window

        # Ensure the application data correctly reflects that monitoring is active
        app_data['auto_monitor'] = True

        # Now, emit the signal to save the application data and clear the form
        self.app_saved.emit(app_data)
        print(f"Application data prepared and emitted after auth: {app_data['school_name']}")
        self.clear_form()
    def __init__(self, parent=None):
        super().__init__(parent)
        self.is_gmail_connected = False
        self.init_ui()
        self.setup_connections()
        self.load_university_names()

    def init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(15)

        title_label = QLabel("Add New Application")
        title_label.setFont(QFont("Segoe UI", 18, QFont.Weight.Bold))
        main_layout.addWidget(title_label)

        scroll_area = QScrollArea(self)
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        form_widget = QWidget()
        self.form_layout = QVBoxLayout(form_widget)
        self.form_layout.setContentsMargins(0, 0, 0, 0)
        self.form_layout.setSpacing(10)

        self.fields = {}

        self.fields['school_name'] = self._create_input_field("School Name:", QLineEdit, "e.g., Cornell University")
        self.school_completer = QCompleter(self)
        self.school_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.school_completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.fields['school_name'].setCompleter(self.school_completer)
        self.form_layout.addWidget(self.fields['school_name'].parentWidget())

        self.fields['application_type'] = self._create_input_field("App Type:", QComboBox)
        self.fields['application_type'].addItems(["ED", "EA", "RD", "REA", "Rolling"])
        self.form_layout.addWidget(self.fields['application_type'].parentWidget())

        self.fields['major'] = self._create_input_field("Major (Optional):", QLineEdit, "e.g., Quantum Physics, ECE")
        self.form_layout.addWidget(self.fields['major'].parentWidget())

        self.fields['deadline'] = self._create_input_field("Deadline:", QDateEdit)
        self.fields['deadline'].setCalendarPopup(True)
        self.fields['deadline'].setDate(QDate.currentDate())
        self.form_layout.addWidget(self.fields['deadline'].parentWidget())

        self.fields['submission_date'] = self._create_input_field("Submission Date:", QDateEdit)
        self.fields['submission_date'].setCalendarPopup(True)
        self.fields['submission_date'].setDate(QDate.currentDate())
        self.form_layout.addWidget(self.fields['submission_date'].parentWidget())

        self.fields['portal_link'] = self._create_input_field("Portal Link (Optional):", QLineEdit,
                                                              "https://apply.school.edu/portal")
        self.form_layout.addWidget(self.fields['portal_link'].parentWidget())

        self.fields['auto_monitor'] = self._create_checkbox("Auto Monitor (via Gmail):")
        self.fields['auto_monitor'].setChecked(True)
        self.form_layout.addWidget(self.fields['auto_monitor'].parentWidget())

        self.fields['notes'] = self._create_input_field("Notes:", QTextEdit,
                                                        "Any personal comments or specific requirements...")
        self.fields['notes'].setFixedHeight(80)
        self.form_layout.addWidget(self.fields['notes'].parentWidget())

        self.form_layout.addStretch(1)

        scroll_area.setWidget(form_widget)
        main_layout.addWidget(scroll_area)

        self.save_button = QPushButton("Add Application")
        self.save_button.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        self.save_button.setStyleSheet("""
            QPushButton {
                background-color: #4CAF50;
                color: white;
                padding: 12px 25px;
                border-radius: 8px;
                border: none;
            }
            QPushButton:hover {
                background-color: #45a049;
            }
            QPushButton:pressed {
                background-color: #3e8e41;
            }
        """)
        main_layout.addWidget(self.save_button, alignment=Qt.AlignmentFlag.AlignCenter)

        self.apply_styles()

    def apply_styles(self):
        self.setStyleSheet("""
            QLabel {
                font-family: 'Segoe UI';
                font-size: 14px;
                color: #E0E0E0;
            }
            QLineEdit, QTextEdit, QComboBox, QDateEdit {
                font-family: 'Segoe UI';
                font-size: 14px;
                background-color: #333333;
                color: #FFFFFF;
                border: 1px solid #555555;
                border-radius: 5px;
                padding: 8px;
            }
            QDateEdit::drop-down {
                width: 20px;
                height: 20px;
                subcontrol-origin: padding;
                subcontrol-position: center right;
                right: 5px;
            }
            QComboBox::drop-down {
                border: 0px;
            }
            QComboBox::down-arrow {
                width: 15px;
                height: 15px;
            }
            QScrollArea {
                border: none;
            }
            QScrollBar:vertical {
                border: none;
                background: #2b2b2b;
                width: 10px;
                margin: 0px 0px 0px 0px;
            }
            QScrollBar::handle:vertical {
                background: #555555;
                min-height: 20px;
                border-radius: 5px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
                background: none;
            }
            QCheckBox::indicator {
                width: 20px;
                height: 20px;
                border: 1px solid #555555;
                border-radius: 4px;
                background-color: #333333;
            }
            QCheckBox::indicator:checked {
                background-color: #4CAF50;
            }
            QFrame#InputFieldContainer {
                background-color: #2b2b2b;
                border-radius: 8px;
                padding: 10px;
            }
        """)

    def _create_input_field(self, label_text, widget_class, placeholder_text=None):
        container = QFrame(self)
        container.setObjectName("InputFieldContainer")
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(5)

        label = QLabel(label_text)
        container_layout.addWidget(label)

        widget = widget_class(self)
        if placeholder_text and isinstance(widget, (QLineEdit, QTextEdit)):
            widget.setPlaceholderText(placeholder_text)

        widget.setFont(QFont("Segoe UI", 12))

        container_layout.addWidget(widget)
        return widget

    def _create_checkbox(self, label_text):
        container = QFrame(self)
        container.setObjectName("InputFieldContainer")
        container_layout = QHBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(10)

        checkbox = QCheckBox(label_text)
        checkbox.setFont(QFont("Segoe UI", 14))
        checkbox.setStyleSheet("QCheckBox { color: #E0E0E0; }")
        container_layout.addWidget(checkbox)
        container_layout.addStretch(1)

        return checkbox

    def load_university_names(self):
        self.university_names = [
            "Harvard University", "Stanford University", "MIT", "Princeton University",
            "Yale University", "Columbia University", "University of Chicago",
            "University of Pennsylvania", "Johns Hopkins University", "California Institute of Technology",
            "Duke University", "Northwestern University", "Dartmouth College",
            "Brown University", "Vanderbilt University", "Rice University",
            "Washington University in St. Louis", "Cornell University", "University of Notre Dame",
            "University of California, Berkeley", "University of California, Los Angeles",
            "Carnegie Mellon University", "Georgetown University", "University of Michigan—Ann Arbor",
            "University of Virginia", "New York University", "University of Southern California",
            "University of Florida", "University of North Carolina—Chapel Hill",
            "Wake Forest University", "Tufts University", "University of California, San Diego",
            "Boston College", "Emory University", "Georgia Institute of Technology",
            "University of Texas at Austin", "University of Wisconsin—Madison",
            "University of Illinois—Urbana-Champaign", "Ohio State University—Columbus",
            "Purdue University—West Lafayette", "University of Washington—Seattle",
            "Stony Brook University", "University of Maryland—College Park",
            "Case Western Reserve University", "Rochester Institute of Technology",
            "Virginia Tech", "UMass Amherst", "UT Dallas"
        ]
        model = QStringListModel()
        model.setStringList(self.university_names)
        self.school_completer.setModel(model)

    def setup_connections(self):
        self.save_button.clicked.connect(self.save_application_prompt_gmail)

    def clear_form(self):
        """Resets all input fields in the application entry form."""
        self.fields['school_name'].clear()
        self.fields['application_type'].setCurrentIndex(0)  # Reset to the first item ("ED")
        self.fields['major'].clear()
        self.fields['deadline'].setDate(QDate.currentDate())
        self.fields['portal_link'].clear()
        self.fields['auto_monitor'].setChecked(True)  # Or False, depending on your desired default
        self.fields['notes'].clear()
        print("Application form cleared.")

        # --- In the ApplicationEntryPanel class ---

        # REPLACE the old save_application_prompt_gmail method with this one
    def save_application_prompt_gmail(self):
        app_data = {
                "school_name": self.fields['school_name'].text().strip(),
                "application_type": self.fields['application_type'].currentText(),
                "major": self.fields['major'].text().strip() if self.fields['major'].text().strip() else None,
                "deadline": self.fields['deadline'].date().toString(Qt.DateFormat.ISODate),
                "submission_date": self.fields['submission_date'].date().toString(Qt.DateFormat.ISODate),
                "portal_link": self.fields['portal_link'].text().strip() if self.fields[
                    'portal_link'].text().strip() else None,
                "auto_monitor": self.fields['auto_monitor'].isChecked(),
                "notes": self.fields['notes'].toPlainText().strip() if self.fields[
                    'notes'].toPlainText().strip() else None,
                "status": "Submitted",
                "result": "Pending",
                "last_checked": QDate.currentDate().toString(Qt.DateFormat.ISODate),
                "timeline": [
                    {"event": "Submitted", "date": QDate.currentDate().toString(Qt.DateFormat.ISODate)}
                ]
        }

        if not app_data["school_name"]:
            QMessageBox.warning(self, "Input Error", "Please enter a School Name.")
            return

            # Case 1: User wants auto-monitoring
        if app_data['auto_monitor']:
            if not self.is_gmail_connected:
                    # Gmail is not connected, so we must authenticate.
                print("Auto-monitor enabled, but Gmail is not connected. Opening auth dialog.")
                gmail_auth_dialog = GmailAuthPanel(self)
                gmail_auth_dialog.auth_successful.connect(lambda: self._handle_gmail_auth_success(app_data))
                gmail_auth_dialog.exec()
            else:
                    # Gmail is already connected, so we can save immediately.
                print("Auto-monitor enabled and Gmail is already connected. Saving.")
                self.app_saved.emit(app_data)
                self.clear_form()

            # Case 2: User does NOT want auto-monitoring
        else:
            print("Auto-monitor disabled. Saving application.")
            self.app_saved.emit(app_data)
            self.clear_form()


# --- ApplicationDashboardPanel Class ---

class ApplicationDashboardPanel(QWidget):
    app_updated = pyqtSignal(str, dict)

    # NEW SIGNALS FOR AI COMMUNICATION
    ai_response_ready = pyqtSignal(str)
    ai_loading_finished = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.applications = []
        self.init_ui()
        self.apply_styles()

        self.resize_timer = QTimer(self)
        self.resize_timer.setInterval(100)
        self.resize_timer.setSingleShot(True)
        self.resize_timer.timeout.connect(self._recalculate_card_layout)

        # Connect the new signals to their slots
        self.ai_response_ready.connect(self._display_ai_response)
        self.ai_loading_finished.connect(self._stop_ai_loading)

    def _display_ai_response(self, response_text):
        """Slot to display AI response from worker thread."""
        self.ai_response_label.setText(response_text)

    def _prompt_decision_result(self, app_id):
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Enter Decision Result")
        dialog.setText(f"Enter the decision result for application ID: {app_id}")

        input_combo = QComboBox(dialog)
        input_combo.addItems(["Accepted", "Rejected", "Waitlisted", "Deferred"])

        layout = QVBoxLayout()
        layout.addWidget(QLabel("Select Result:"))
        layout.addWidget(input_combo)
        dialog.layout().addLayout(layout)  # Add to existing QMessageBox layout

        dialog.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)

        ret = dialog.exec()
        if ret == QMessageBox.StandardButton.Ok:
            selected_result = input_combo.currentText()
            self.app_updated.emit(app_id, {"result": selected_result, "status": "Decision Processed"})

    def toggle_monitor(self, app_id: str, enable: bool):

        # Always emit; handle_app_update will open the Gmail dialog if needed
        self.app_updated.emit(app_id, {"auto_monitor": enable})
    def _confirm_toggle(self, app_id, enable):
        """Safely emit the toggle change to the main window."""
        print(f"[Toggle] App ID {app_id} set to auto_monitor={enable}")
        main_window = self.window()  # top-level CombinedApp
        if hasattr(main_window, "app_updated"):
            main_window.app_updated.emit(app_id, {"auto_monitor": enable})
        else:
            print("Warning: main window signal not found")
    def _reset_checkbox_for(self, app_id, state):
        for i in range(self.table.rowCount()):
            try:
                checkbox = self.table.cellWidget(i, 5)
                if checkbox and checkbox.property("app_id") == app_id:
                    from PyQt6.QtCore import QSignalBlocker
                    blocker = QSignalBlocker(checkbox)
                    checkbox.setChecked(state)
                    print(f"[Reset] Checkbox for app_id={app_id} reset to {state}")
                    return
            except Exception as e:
                print(f"[ERROR] Failed resetting checkbox for app_id={app_id}: {e}")

    def init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(15)

        # Title
        title_label = QLabel("Your Applications Dashboard")
        title_label.setFont(QFont("Segoe UI", 18, QFont.Weight.Bold))
        title_label.setStyleSheet("color: white;")
        main_layout.addWidget(title_label)

        # Insights Bar
        self.insights_area = QFrame(self)
        self.insights_area.setObjectName("InsightsArea")
        self.insights_area.setStyleSheet("""
            QFrame#InsightsArea {
            background-color: #1e1e1e;
            border-radius: 10px;
            padding: 10px;
            }
            QLabel {
            color: white;
            font-size: 13px;
            }
        """)
        insights_layout = QHBoxLayout(self.insights_area)
        insights_layout.setContentsMargins(20, 15, 20, 15)
        insights_layout.setSpacing(30)

        self.apps_submitted_label = self._create_insight_label("🔢 Apps Submitted: 0")
        self.awaiting_decision_label = self._create_insight_label("🟢 Awaiting: 0")
        self.accepted_label = self._create_insight_label("✅ Accepted: 0")
        self.rejected_label = self._create_insight_label("❌ Rejected: 0")
        self.avg_time_label = self._create_insight_label("⏱️ Avg Decision Time: N/A")
        self.likeliest_outcome_label = self._create_insight_label("🎓 Next Likely: N/A")

        for label in [
            self.apps_submitted_label, self.awaiting_decision_label,
            self.accepted_label, self.rejected_label,
            self.avg_time_label, self.likeliest_outcome_label
        ]:
            insights_layout.addWidget(label)

        insights_layout.addStretch(1)
        main_layout.addWidget(self.insights_area)

        # Scrollable Application Cards Grid
        self.dashboard_scroll_area = QScrollArea(self)
        self.dashboard_scroll_area.setWidgetResizable(True)

        from PyQt6.QtCore import Qt
        self.dashboard_scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.dashboard_scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        self.dashboard_container = QWidget()
        self.dashboard_grid_layout = QGridLayout(self.dashboard_container)
        self.dashboard_grid_layout.setContentsMargins(10, 10, 10, 10)
        self.dashboard_grid_layout.setSpacing(20)

        self.dashboard_scroll_area.setWidget(self.dashboard_container)
        main_layout.addWidget(self.dashboard_scroll_area, stretch=1)

        # AI Assistant Section
        self.ai_question_frame = QFrame(self)
        self.ai_question_frame.setObjectName("AIQuestionFrame")
        self.ai_question_frame.setStyleSheet("""
            QFrame#AIQuestionFrame {
            background-color: #1e1e1e;
            border-radius: 10px;
            padding: 15px;
            }
        """)
        ai_question_layout = QVBoxLayout(self.ai_question_frame)
        ai_question_layout.setContentsMargins(15, 10, 15, 10)
        ai_question_layout.setSpacing(10)

        ai_question_title = QLabel("Application Insights AI")
        ai_question_title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        ai_question_title.setStyleSheet("color: #88c0ff;")
        ai_question_layout.addWidget(ai_question_title)

        self.ai_question_input = QLineEdit()
        self.ai_question_input.setPlaceholderText("Ask about your applications (e.g., 'Where did I get accepted?')")
        self.ai_question_input.setStyleSheet("""
            QLineEdit {
            background: #2b2b2b;
            color: white;
            border: 1px solid #444;
            border-radius: 6px;
            padding: 8px;
            font-size: 13px;
            }
            QLineEdit:focus {
            border: 1px solid #007bff;
            }
        """)
        self.ai_question_input.returnPressed.connect(
            lambda: self._ask_ai_question(self.ai_question_input.text())
        )
        ai_question_layout.addWidget(self.ai_question_input)

        self.ai_response_label = QLabel("Waiting for your question...")
        self.ai_response_label.setWordWrap(True)
        self.ai_response_label.setStyleSheet("color: #ccc; font-size: 12px; padding: 5px;")
        ai_question_layout.addWidget(self.ai_response_label)

        self.ai_loading_movie = QMovie("loading.gif")
        if not self.ai_loading_movie.isValid():
            print("WARNING: loading.gif not found. Animation disabled.")
            self.ai_loading_movie = None

        self.ai_loading_label = QLabel()
        if self.ai_loading_movie:
            self.ai_loading_label.setMovie(self.ai_loading_movie)
        self.ai_loading_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.ai_loading_label.setVisible(False)
        ai_question_layout.addWidget(self.ai_loading_label)

        main_layout.addWidget(self.ai_question_frame)
        main_layout.addStretch(1)

    def _ask_ai_question(self, question):
        if not question:
            self.ai_response_ready.emit("Please enter a question.")
            return

        self.ai_question_input.setEnabled(False)
        self.ai_response_ready.emit("Thinking...")  # Use signal here too
        if self.ai_loading_movie:
            self.ai_loading_label.setVisible(True)
            self.ai_loading_movie.start()

        app_summary = []
        for app in self.applications:
            summary = {
                "school_name": app.get("school_name", "N/A"),
                "status": app.get("status", "Unknown"),
                "result": app.get("result", "Pending"),
                "major": app.get("major", "N/A"),
                "deadline": app.get("deadline", "N/A"),
                "submission_date": app.get("submission_date", "N/A"),
            }
            app_summary.append(summary)

        applications_text = json.dumps(app_summary, indent=2)

        panel_instance = self  # Get a safe reference to self

        def worker():
            try:
                prompt = (
                    f"You are an AI assistant designed to help analyze college applications. "
                    f"Here is a list of my current college applications:\n\n"
                    f"{applications_text}\n\n"
                    f"Based on this data, please answer the following question concisely and clearly: '{question}'\n"
                    f"Keep your answer to a maximum of 1-3 sentences. Avoid conversational filler. Your response should be addressing the applicant directly."
                )

                # Retrieve the API key securely again, just in case
                current_gemini_api_key = "AIzaSyD-OVmWRATEH5e18Y06c192Cbn0igs3e3E"
                if not current_gemini_api_key:
                    panel_instance.ai_response_ready.emit(
                        "Error: Gemini API Key (GEMINI_API_KEY) is not set in environment variables."
                    )
                    return  # Exit worker thread early

                # Ensure the model is configured for this thread if necessary,
                # or rely on the global configuration if it's guaranteed to be set.
                # For robustness, re-configuring here is safer if threads might lose context.
                genai.configure(api_key=current_gemini_api_key)

                # Ensure model is not None before calling
                if model is None:
                    panel_instance.ai_response_ready.emit("Error: Gemini model not initialized. API key missing.")
                    return

                response = model.generate_content(prompt)
                ai_response = response.text.strip()
                panel_instance.ai_response_ready.emit(ai_response)
            except Exception as e:
                panel_instance.ai_response_ready.emit(f"Error: Could not get AI response. {e}")
            finally:
                panel_instance.ai_loading_finished.emit()

        threading.Thread(target=worker).start()

    def _stop_ai_loading(self):
        if self.ai_loading_movie:
            self.ai_loading_movie.stop()
            self.ai_loading_label.setVisible(False)
        self.ai_question_input.setEnabled(True)
        self.ai_question_input.clear()  # Clear input after response

    def apply_styles(self):
        self.setStyleSheet("""
        #InsightsArea {
            background-color: #2a2a2a;
            border-radius: 10px;
        }
        #AIQuestionFrame {
            background-color: #1e1e1e;
            border: 1px solid #333;
            border-radius: 10px;
        }
        #SchoolName {
            font-size: 15px;
            font-weight: bold;
            color: #f0f0f0;
        }
        #StatusLabel {
            font-size: 13px;
            font-weight: bold;
        }
        #Link {
            color: #61dafb;
        }
        #ApplicationCard {
            background-color: #1c1c1c;
            border-radius: 12px;
        }
        """)

    def _create_insight_label(self, text):
        label = QLabel(text)
        label.setStyleSheet("color: #e0e0e0; font-size: 13px; font-weight: bold;")
        label.setWordWrap(True)
        return label

    def resizeEvent(self, event):
        self.resize_timer.start()
        super().resizeEvent(event)

    def _recalculate_card_layout(self):
        # --- 1. Safe teardown: disconnect + delete every widget ---
        for i in reversed(range(self.dashboard_grid_layout.count())):
            item = self.dashboard_grid_layout.itemAt(i)
            if item and item.widget():
                w = item.widget()
                try:
                    w.disconnect()  # break all Qt signal/slot connections
                except Exception:
                    pass  # nothing connected – safe to ignore
                w.deleteLater()  # schedule real C++ deletion
            self.dashboard_grid_layout.removeItem(item)

        # --- 2. Responsive column count ---
        max_cols = max(1, self.width() // 380)
        min_card_height = 260

        # --- 3. Empty-state placeholder ---
        if not self.applications:
            no_apps_label = QLabel("No applications tracked yet.")
            no_apps_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            no_apps_label.setStyleSheet("color: #999999; font-size: 14px; padding: 50px;")
            self.dashboard_grid_layout.addWidget(no_apps_label, 0, 0, 1, max_cols)
            self.dashboard_grid_layout.setRowStretch(0, 1)
            self.dashboard_grid_layout.setRowMinimumHeight(0, min_card_height)
            self._update_insights()
            return

        # --- 4. Populate new cards ---
        row, col = 0, 0
        for app_data in sorted(self.applications, key=lambda x: x.get("school_name", "")):
            card = self._create_application_card(app_data)
            self.dashboard_grid_layout.addWidget(card, row, col)
            self.dashboard_grid_layout.setRowMinimumHeight(row, min_card_height)
            col += 1
            if col >= max_cols:
                col = 0
                row += 1

        # --- 5. Final spacer so cards stay at the top ---
        self.dashboard_grid_layout.addItem(
            QSpacerItem(0, 0, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding),
            row + 1, 0, 1, max_cols
        )

        self.dashboard_container.adjustSize()
        self._update_insights()
    @pyqtSlot(list)
    def update_dashboard(self, applications: list):
        self.applications = applications
        self._recalculate_card_layout()
        self._update_insights()

    def _create_application_card(self, app_data):
        card = QFrame()
        card.setObjectName("ApplicationCard")
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.MinimumExpanding)

        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(1)
        shadow.setXOffset(1)
        shadow.setYOffset(1)
        shadow.setColor(QColor(0, 0, 0, 80))
        card.setGraphicsEffect(shadow)

        card.hover_anim = QPropertyAnimation(shadow, b"blurRadius")
        card.hover_anim.setDuration(150)
        card.hover_anim.setEasingCurve(QEasingCurve.Type.OutQuad)

        card.offset_anim_x = QPropertyAnimation(shadow, b"xOffset")
        card.offset_anim_y = QPropertyAnimation(shadow, b"yOffset")
        card.offset_anim_x.setDuration(150)
        card.offset_anim_y.setDuration(150)
        card.offset_anim_x.setEasingCurve(QEasingCurve.Type.OutQuad)
        card.offset_anim_y.setEasingCurve(QEasingCurve.Type.OutQuad)

        def enter_event(event):
            card.hover_anim.setStartValue(shadow.blurRadius())
            card.hover_anim.setEndValue(20)
            card.offset_anim_x.setStartValue(shadow.xOffset())
            card.offset_anim_x.setEndValue(5)
            card.offset_anim_y.setStartValue(shadow.yOffset())
            card.offset_anim_y.setEndValue(5)
            card.hover_anim.start()
            card.offset_anim_x.start()
            card.offset_anim_y.start()

        def leave_event(event):
            card.hover_anim.setStartValue(shadow.blurRadius())
            card.hover_anim.setEndValue(1)
            card.offset_anim_x.setStartValue(shadow.xOffset())
            card.offset_anim_x.setEndValue(1)
            card.offset_anim_y.setStartValue(shadow.yOffset())
            card.offset_anim_y.setEndValue(1)
            card.hover_anim.start()
            card.offset_anim_x.start()
            card.offset_anim_y.start()

        card.enterEvent = enter_event
        card.leaveEvent = leave_event

        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)

        top = QHBoxLayout()
        school = QLabel(f"🎓 {app_data.get('school_name', 'N/A')}")
        school.setObjectName("SchoolName")
        school.setWordWrap(True)
        top.addWidget(school, stretch=1)

        result = app_data.get("result", "Pending")
        status = app_data.get("status", "Unknown")
        status_label = QLabel(status)
        status_label.setObjectName("StatusLabel")
        status_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        status_label.setWordWrap(True)

        # Color coding
        color_map = {
            "Accepted": "#4CAF50",
            "Rejected": "#FF6347",
            "Waitlisted": "#FFA500",
            "Deferred": "#87CEEB",
        }
        status_label.setStyleSheet(f"color: {color_map.get(result, '#ADD8E6')};")
        top.addWidget(status_label)
        layout.addLayout(top)

        if result != "Pending" and status == "Decision Released":
            res_label = QLabel(f"Result: <b>{result}</b>")
            res_label.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
            res_label.setStyleSheet(status_label.styleSheet())
            layout.addWidget(res_label)

        # Core info
        for key, label in {
            "major": "Major",
            "application_type": "Type",
            "submission_date": "Submitted",
            "deadline": "Deadline",
            "last_checked": "Last Monitored"
        }.items():
            val = app_data.get(key, "N/A")
            qlabel = QLabel(f"{label}: {val}")
            qlabel.setStyleSheet("color: #CCC; font-size: 13px;")
            qlabel.setWordWrap(True)
            layout.addWidget(qlabel)

        # Portal link
        if app_data.get("portal_link"):
            link = QLabel(f"<a href='{app_data['portal_link']}'>View Portal</a>")
            link.setOpenExternalLinks(True)
            link.setObjectName("Link")
            link.setFont(QFont("Segoe UI", 13))
            layout.addWidget(link)

        # Auto monitor checkbox
        # --- inside _create_application_card() ---
        mon_layout = QHBoxLayout()

        # 1. create the checkbox
        toggle = QCheckBox("Auto Monitor")
        toggle.setChecked(app_data.get("auto_monitor", False))
        toggle.setStyleSheet("QCheckBox { color: #E0E0E0; font-size: 13px; }")

        # 2. **store its *ID* instead of the widget pointer**
        toggle.setProperty("app_id", app_data["id"])

        # 3. **single-argument lambda** — no QObject pointer captured
        toggle.stateChanged.connect(
            lambda state, aid=app_data["id"]: self.toggle_monitor(aid, bool(state))
        )

        mon_layout.addWidget(toggle)
        mon_layout.addStretch(1)
        # Delete button
        delete_btn = QPushButton("🗑 Remove")
        delete_btn.setStyleSheet("""
            QPushButton {
                background-color: #d9534f;
                color: white;
                padding: 6px 12px;
                border-radius: 6px;
            }
            QPushButton:hover {
                background-color: #c9302c;
            }
        """)
        delete_btn.clicked.connect(lambda _, id=app_data.get("id"): self._remove_application(id))
        mon_layout.addWidget(delete_btn)
        layout.addLayout(mon_layout)

        # Optionally show "Enter Decision" if released but still pending
        if status == "Decision Released" and result in ["Pending", "Deferred", "Waitlisted"]:
            enter_btn = QPushButton("Enter Decision Result")
            enter_btn.setStyleSheet("""
                QPushButton {
                    background-color: #007bff;
                    color: white;
                    padding: 8px 15px;
                    border-radius: 5px;
                    border: none;
                }
                QPushButton:hover { background-color: #0056b3; }
            """)
            enter_btn.clicked.connect(lambda: self._prompt_decision_result(app_data.get("id")))
            layout.addWidget(enter_btn)

        return card



    def _remove_application(self, app_id):
        """Ask for confirmation and, if accepted, delete the application."""
        reply = QMessageBox.question(
            self,
            "Confirm Deletion",
            "Are you sure you want to remove this application?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        # ... inside _remove_application
        if reply == QMessageBox.StandardButton.Yes:
            # The 'app_updated' signal is part of this class (self),
            # not the main_window.
            self.app_updated.emit("delete", {"id": app_id})
    def _update_insights(self):
        total = len(self.applications)
        awaiting = sum(1 for a in self.applications if a.get("result") in ["Pending", "Deferred", "Waitlisted"])
        accepted = sum(1 for a in self.applications if a.get("result") == "Accepted")
        rejected = sum(1 for a in self.applications if a.get("result") == "Rejected")
        waitlisted = sum(1 for a in self.applications if a.get("result") == "Waitlisted")
        deferred = sum(1 for a in self.applications if a.get("result") == "Deferred")

        self.apps_submitted_label.setText(f"🔢 Apps Submitted: {total}")
        self.awaiting_decision_label.setText(f"🟢 Awaiting: {awaiting}")
        self.accepted_label.setText(f"✅ Accepted: {accepted}")
        self.rejected_label.setText(f"❌ Rejected: {rejected}")

        decision_times = []
        for app in self.applications:
            sub = app.get("submission_date")
            release_event = next((e for e in app.get("timeline", []) if e.get("event") == "Decision Released"), None)
            if sub and release_event:
                try:
                    sub_date = datetime.strptime(sub, "%Y-%m-%d")
                    dec_date = datetime.strptime(release_event.get("date"), "%Y-%m-%d")
                    decision_times.append((dec_date - sub_date).days)
                except Exception:
                    pass

        if decision_times:
            avg_days = sum(decision_times) / len(decision_times)
            self.avg_time_label.setText(f"⏱️ Avg Decision Time: {avg_days:.1f} days")
        else:
            self.avg_time_label.setText("⏱️ Avg Decision Time: N/A")

        if awaiting:
            if accepted > rejected and accepted > waitlisted and accepted > deferred:
                self.likeliest_outcome_label.setText("🎓 Next Likely: Another Acceptance!")
            elif deferred > 0:
                self.likeliest_outcome_label.setText("🎓 Next Likely: Deferred - Still in Play!")
            elif waitlisted > 0:
                self.likeliest_outcome_label.setText("🎓 Next Likely: Waitlisted - Stay Hopeful!")
            else:
                self.likeliest_outcome_label.setText(f"🎓 Next Likely: Awaiting {awaiting} decisions")
        else:
            self.likeliest_outcome_label.setText("🎓 Next Likely: All decisions received!")



class CombinedApp(QMainWindow):
    update_app_dashboard = pyqtSignal(list)
    explainer_response_ready = pyqtSignal(dict)
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Pathwise – Career & Academic AI")
        self.resize(1200, 800)

        self.current_mode = "career"
        self.history = self.load_history()  # For career mode
        self.explainer_data = self.load_explainer_data()  # For explainer mode
        self.applications_db_file = "applications.json"  # DB file for applications
        self.applications = self.load_applications()  # Load applications
        self.gmail_monitor = None
        self.open_cards = []

        self.load_theme()
        self.load_fonts()
        self.build_base_ui()
        self.switch_to_career()

        cip_path = "C:/Users/iqbal/Downloads/cip_codes.json"
        if os.path.exists(cip_path):
            with open(cip_path, "r") as f:
                self.cip_list = json.load(f)
                self.cip_titles = [item["title"] for item in self.cip_list]
        else:
            print(f"WARNING: cip_codes.json not found at {cip_path}. College matching by major may be limited.")
            self.cip_list = []
            self.cip_titles = []
        self.app_entry_panel.is_gmail_connected = os.path.exists(TOKEN_FILE)

    def update_top_bar_buttons(self):
        for i in reversed(range(self.top_bar.count())):
            widget = self.top_bar.itemAt(i).widget()
            if widget and widget != self.sectionLabel:
                self.top_bar.removeWidget(widget)
                widget.deleteLater()

        self.top_bar.addStretch()

        if self.current_mode != "career":
            btn = QPushButton("Career Counselor")
            btn.setFont(QFont("TypoRoundRegularDemo", 12))
            btn.clicked.connect(self.switch_to_career)
            self.top_bar.addWidget(btn)
        if self.current_mode != "explainer":
            btn = QPushButton("Academic Explainer")
            btn.setFont(QFont("TypoRoundRegularDemo", 12))
            btn.clicked.connect(self.switch_to_explainer)
            self.top_bar.addWidget(btn)
        if self.current_mode != "college_match":
            btn = QPushButton("College Match Engine")
            btn.setFont(QFont("TypoRoundRegularDemo", 12))
            btn.clicked.connect(self.switch_to_college_match)
            self.top_bar.addWidget(btn)
        if self.current_mode != "application_tracker":
            btn = QPushButton("📬 Application Tracker")
            btn.setFont(QFont("TypoRoundRegularDemo", 12))
            btn.clicked.connect(self.switch_to_application_tracker)
            self.top_bar.addWidget(btn)

        for i in range(self.top_bar.count()):
            widget = self.top_bar.itemAt(i).widget()
            if isinstance(widget, QPushButton):
                widget.setFixedWidth(220)
                widget.setStyleSheet("""
                    QPushButton {
                        padding: 8px;
                        border-radius: 8px;
                        background: #333;
                        color: white;
                        border: 1px solid #444;
                    }
                    QPushButton:hover {
                        background: #444;
                        border: 1px solid #555;
                    }
                    QPushButton:pressed {
                        background: #222;
                        border: 1px solid #333;
                    }
                """)

    def load_theme(self):
        try:
            with open("settings.json", "r") as f:
                self.current_theme = json.load(f).get("theme", "dark")
        except FileNotFoundError:
            self.current_theme = "dark"

    def load_fonts(self):
        if os.path.exists("TypoRoundRegularDemo.ttf"):
            QFontDatabase.addApplicationFont("TypoRoundRegularDemo.ttf")
        if os.path.exists("Black Brownies.ttf"):
            QFontDatabase.addApplicationFont("Black Brownies.ttf")

    def build_base_ui(self):
        self.main_widget = QWidget()
        self.main_layout = QVBoxLayout(self.main_widget)
        self.setCentralWidget(self.main_widget)

        self.top_bar = QHBoxLayout()
        self.sectionLabel = QLabel()
        self.sectionLabel.setStyleSheet("font-size: 18px; font-weight: bold; color: white;")
        self.top_bar.addWidget(self.sectionLabel)

        self.main_layout.addLayout(self.top_bar)

        self.stack = QStackedWidget()
        self.build_career_result_ui()

        self.build_career_ui()
        self.build_explainer_ui()
        self.build_college_match_ui()
        self.build_application_tracker_ui()

        self.stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.main_layout.addWidget(self.stack)
        self.update_top_bar_buttons()

    # --- Application Tracker UI & Logic ---
    def switch_to_application_tracker(self):
        self.current_mode = "application_tracker"
        self.sectionLabel.setText("Application Tracker")
        self.stack.setCurrentWidget(self.application_tracker_ui)
        self.update_top_bar_buttons()
        self.app_dashboard_panel.update_dashboard(self.applications)

    def build_application_tracker_ui(self):
        if hasattr(self, 'application_tracker_ui'):
            return

        self.application_tracker_ui = QWidget()
        main_layout = QVBoxLayout(self.application_tracker_ui)

        self.tracker_mode_switcher = QHBoxLayout()
        self.add_app_btn = QPushButton("Add Application")
        self.view_dashboard_btn = QPushButton("View Dashboard")

        self.add_app_btn.setStyleSheet("""
            QPushButton {
                background-color: #555555;
                color: white;
                padding: 10px 20px;
                border-radius: 8px;
            }
            QPushButton:hover { background-color: #666666; }
            QPushButton:checked { background-color: #007bff; border: 1px solid #0056b3; }
        """)
        self.view_dashboard_btn.setStyleSheet(self.add_app_btn.styleSheet())

        self.add_app_btn.setCheckable(True)
        self.view_dashboard_btn.setCheckable(True)
        self.add_app_btn.setChecked(True)

        self.tracker_mode_switcher.addStretch(1)
        self.tracker_mode_switcher.addWidget(self.add_app_btn)
        self.tracker_mode_switcher.addWidget(self.view_dashboard_btn)
        self.tracker_mode_switcher.addStretch(1)
        main_layout.addLayout(self.tracker_mode_switcher)

        self.tracker_stacked_widget = QStackedWidget()

        self.app_entry_panel = ApplicationEntryPanel()
        self.app_dashboard_panel = ApplicationDashboardPanel()

        self.tracker_stacked_widget.addWidget(self.app_entry_panel)
        self.tracker_stacked_widget.addWidget(self.app_dashboard_panel)

        main_layout.addWidget(self.tracker_stacked_widget)

        self.add_app_btn.clicked.connect(lambda: self.tracker_stacked_widget.setCurrentWidget(self.app_entry_panel))
        self.add_app_btn.clicked.connect(lambda: self.add_app_btn.setChecked(True))
        self.add_app_btn.clicked.connect(lambda: self.view_dashboard_btn.setChecked(False))

        self.view_dashboard_btn.clicked.connect(
            lambda: self.tracker_stacked_widget.setCurrentWidget(self.app_dashboard_panel))
        self.view_dashboard_btn.clicked.connect(lambda: self.view_dashboard_btn.setChecked(True))
        self.view_dashboard_btn.clicked.connect(lambda: self.add_app_btn.setChecked(False))
        self.view_dashboard_btn.clicked.connect(lambda: self.app_dashboard_panel.update_dashboard(self.applications))

        self.app_entry_panel.app_saved.connect(self.add_application_to_db)
        self.app_entry_panel.gmail_connected.connect(self.set_gmail_connected_status)
        self.app_dashboard_panel.app_updated.connect(self.handle_app_update)
        self.update_app_dashboard.connect(self.app_dashboard_panel.update_dashboard)

        self.stack.addWidget(self.application_tracker_ui)

        # In Pathwise.py, inside the CombinedApp class

        # REPLACE the old handle_app_update method with this one:
    @pyqtSlot(str, dict)
    def handle_app_update(self, action, data):
        """Handles deleting or updating an application from the dashboard."""

        # --- PATH FOR DELETING AN APPLICATION ---
        if action == "delete":
            app_id_to_delete = data.get("id")
            if not app_id_to_delete:
                return

            initial_count = len(self.applications)
                # Create a new list excluding the application to be deleted
            self.applications = [app for app in self.applications if app.get("id") != app_id_to_delete]

                # If the list is now shorter, the deletion was successful
            if len(self.applications) < initial_count:
                print(f"Application deleted for ID {app_id_to_delete}. Saving and refreshing.")
                self.save_applications()
                self.update_app_dashboard.emit(self.applications)  # This refreshes the UI
            else:
                print(f"Warning: Could not find app with ID {app_id_to_delete} to delete.")

        # --- PATH FOR UPDATING AN APPLICATION (e.g., toggling monitor) ---
        else:
            app_id_to_update = action  # For updates, the action is the ID
            app_found = False
            for app in self.applications:
                if app.get("id") == app_id_to_update:
                    app.update(data)  # Update the dictionary with new data
                    app_found = True
                    # Add result to timeline if applicable
                    if 'result' in data:
                        app.setdefault("timeline", []).append({
                            "event": f"Result Entered: {data['result']}",
                            "date": QDate.currentDate().toString(Qt.DateFormat.ISODate)
                        })
                    break

            if app_found:
                print(f"Application updated for ID {app_id_to_update}. Saving and refreshing.")
                self.save_applications()
                self.update_app_dashboard.emit(self.applications)  # This refreshes the UI
            else:
                print(f"Warning: Could not find app with ID {app_id_to_update} to update.")

        # If Gmail is connected, ensure the monitor has the latest app list
        if self.app_entry_panel.is_gmail_connected:
            self._start_gmail_monitor()
    def set_gmail_connected_status(self, status: bool):
        self.app_entry_panel.is_gmail_connected = status
        print(f"Global Gmail connected status updated to: {status}")
        if status:
            self._start_gmail_monitor()
    def _start_gmail_monitor(self):
        if self.gmail_monitor is None:
            self.gmail_monitor = GmailMonitor(self.applications)
            self.gmail_monitor.result_found.connect(self.update_application_result)
        else:
            self.gmail_monitor.reload_apps(self.applications)
        self.gmail_monitor.start()

    def _stop_gmail_monitor(self):
        if self.gmail_monitor:
            self.gmail_monitor.stop()
    def add_application_to_db(self, app_data: dict):
        app_id_candidate = f"{app_data['school_name'].replace(' ', '_').lower()}_{app_data['submission_date']}"
        existing_ids = {app.get('id') for app in self.applications}
        if app_id_candidate in existing_ids:
            counter = 1
            while f"{app_id_candidate}_{counter}" in existing_ids:
                counter += 1
            app_data["id"] = f"{app_id_candidate}_{counter}"
        else:
            app_data["id"] = app_id_candidate

        self.applications.append(app_data)
        self.save_applications()
        self.update_app_dashboard.emit(self.applications)


        if self.app_entry_panel.is_gmail_connected:
            self._start_gmail_monitor()

    def _send_app_to_n8n(self, app_data):
        n8n_app_webhook_url = os.getenv("N8N_APP_WEBHOOK_URL", "https://glurgle.app.n8n.cloud/webhook/c251ffa0-6032-4635-b685-1e348d156400")
        if "your.n8n.instance" in n8n_app_webhook_url:
            print("WARNING: n8n application webhook URL not configured. Skipping data send.")
            QMessageBox.warning(self, "n8n Config Missing",
                                "n8n webhook URL for applications is not set. Auto-monitoring will not work.")
            return

        try:
            payload = {
                "id": app_data["id"],
                "school_name": app_data["school_name"],
                "portal_link": app_data["portal_link"],
                "auto_monitor": app_data["auto_monitor"],
                "app_type": app_data["application_type"],
                "submission_date": app_data["submission_date"],
                "deadline": app_data["deadline"],
                "school_domains": self._get_school_email_domains(app_data["school_name"])
            }

            response = requests.post(n8n_app_webhook_url, json=payload, timeout=5)
            response.raise_for_status()
            print(f"Successfully sent new application data to n8n for {app_data['school_name']}.")
        except requests.exceptions.RequestException as e:
            print(f"Error sending new app data to n8n for {app_data['school_name']}: {e}")
            QMessageBox.critical(self, "n8n Connection Error",
                                 f"Failed to send application to n8n for auto-monitoring: {e}")

    def _get_school_email_domains(self, school_name):
        domain_map = {
            "Harvard University": ["harvard.edu", "college.harvard.edu", "fas.harvard.edu"],
            "Stanford University": ["stanford.edu", "admissions.stanford.edu"],
            "MIT": ["mit.edu", "admissions.mit.edu"],
            "Princeton University": ["princeton.edu", "admissions.princeton.edu"],
            "Yale University": ["yale.edu", "admissions.yale.edu"],
            "Columbia University": ["columbia.edu", "admissions.columbia.edu"],
            "University of Pennsylvania": ["upenn.edu", "admissions.upenn.edu"],
            "Cornell University": ["cornell.edu", "admissions.cornell.edu", "cals.cornell.edu"],
            "University of Michigan—Ann Arbor": ["umich.edu", "admissions.umich.edu"],
            "Georgia Institute of Technology": ["gatech.edu", "admission.gatech.edu"],
            "Rice University": ["rice.edu", "admission.rice.edu"],
            "University of Maryland—College Park": ["umd.edu", "admissions.umd.edu"],
            "University of Washington—Seattle": ["uw.edu", "admissions.uw.edu"],
            "UT Dallas": ["utdallas.edu", "admissions.utdallas.edu"],
            "Stony Brook University": ["stonybrook.edu", "admissions.stonybrook.edu"],
            "Case Western Reserve University": ["case.edu", "case.edu"],
            "Rochester Institute of Technology": ["rit.edu", "rit.edu"],
            "Virginia Tech": ["vt.edu", "vt.edu"],
            "UMass Amherst": ["umass.edu", "umass.edu"],
        }
        return domain_map.get(school_name, [f"{school_name.lower().replace(' ', '')}.edu"])

    @pyqtSlot(str, bool)
    def update_application_monitor_status(self, app_id: str, enabled: bool):
        found = False
        for app in self.applications:
            if app.get("id") == app_id:
                app["auto_monitor"] = enabled
                found = True
                break

        if found:
            self.save_applications()
            self.update_app_dashboard.emit(self.applications)
            print(f"Updated monitor status for app ID {app_id} to {enabled}. (n8n update pending)")
        else:
            print(f"Application with ID {app_id} not found for monitor status update.")

    @pyqtSlot(str, str)
    def update_application_result(self, app_id: str, result: str):
        found = False
        for app in self.applications:
            if app.get("id") == app_id:
                app["result"] = result
                app["status"] = "Decision Processed" if result not in ["Pending", "Deferred",
                                                                       "Waitlisted"] else "Decision Released"
                app["timeline"].append(
                    {"event": f"Result Entered: {result}", "date": QDate.currentDate().toString(Qt.DateFormat.ISODate)})
                found = True
                break

        if found:
            self.save_applications()
            self.update_app_dashboard.emit(self.applications)
            print(f"Updated result for app ID {app_id} to {result}.")
        else:
            print(f"Application with ID {app_id} not found for result update.")

    def load_applications(self):
        if os.path.exists(self.applications_db_file):
            try:
                with open(self.applications_db_file, "r") as f:
                    return json.load(f)
            except json.JSONDecodeError:
                print(f"Error reading {self.applications_db_file}, starting fresh.")
                return []
        return []

    def save_applications(self):
        with open(self.applications_db_file, "w") as f:
            json.dump(self.applications, f, indent=2)

    def switch_to_college_match(self):
        self.current_mode = "college_match"
        self.sectionLabel.setText("College Match Engine")
        self.stack.setCurrentWidget(self.match_ui)
        self.update_top_bar_buttons()

    def build_college_match_ui(self):
        if hasattr(self, 'match_ui'):
            return

        self.match_ui = QWidget()
        layout = QVBoxLayout(self.match_ui)

        filter_grid = QGridLayout()
        filter_grid.setHorizontalSpacing(12)
        filter_grid.setVerticalSpacing(8)

        input_style = """
            QLineEdit, QComboBox {
                background: rgba(255, 255, 255, 0.08);
                color: white;
                padding: 8px;
                border-radius: 6px;
                border: 1px solid rgba(255, 255, 255, 0.15);
                font-size: 13px;
            }
            QLineEdit:focus, QComboBox:focus {
                border: 1px solid #88c0ff;
            }
            QLabel {
                color: #E0E0E0;
                font-size: 13px;
            }
        """

        self.state_input = QLineEdit()
        self.state_input.setPlaceholderText("State (e.g. NY)")
        self.state_input.setStyleSheet(input_style)

        self.sat_min_input = QLineEdit()
        self.sat_min_input.setPlaceholderText("Min SAT")
        self.sat_min_input.setStyleSheet(input_style)

        self.sat_max_input = QLineEdit()
        self.sat_max_input.setPlaceholderText("Max SAT")
        self.sat_max_input.setStyleSheet(input_style)

        self.sort_field = QComboBox()
        self.sort_field.addItems(["None", "SAT Score", "Admission Rate", "Student Size", "College Name"])
        self.sort_field.setStyleSheet(input_style)

        self.sort_order = QComboBox()
        self.sort_order.addItems(["Descending", "Ascending"])
        self.sort_order.setStyleSheet(input_style)

        self.school_type = QComboBox()
        self.school_type.addItems(["All", "Public", "Private"])
        self.school_type.setStyleSheet(input_style)

        filter_grid.addWidget(QLabel("School Type:"), 0, 0)
        filter_grid.addWidget(self.school_type, 0, 1)

        filter_grid.addWidget(QLabel("Sort By:"), 0, 2)
        filter_grid.addWidget(self.sort_field, 0, 3)

        filter_grid.addWidget(QLabel("Order:"), 0, 4)
        filter_grid.addWidget(self.sort_order, 0, 5)

        filter_grid.addWidget(QLabel("State:"), 1, 0)
        filter_grid.addWidget(self.state_input, 1, 1)

        filter_grid.addWidget(QLabel("Min SAT:"), 1, 2)
        filter_grid.addWidget(self.sat_min_input, 1, 3)

        filter_grid.addWidget(QLabel("Max SAT:"), 1, 4)
        filter_grid.addWidget(self.sat_max_input, 1, 5)

        layout.addLayout(filter_grid)

        fetch_button = QPushButton("Find Matches")
        fetch_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        fetch_button.setFixedHeight(42)
        fetch_button.setStyleSheet("""
            QPushButton {
                background-color: #3457D5;
                color: white;
                font-size: 16px;
                font-weight: bold;
                padding: 10px 20px;
                border-radius: 8px;
            }
            QPushButton:hover {
                background-color: #5ba2d5;
            }
        """)

        fetch_button.clicked.connect(self.run_college_match)
        layout.addWidget(fetch_button)

        self.result_scroll = QScrollArea()
        self.result_scroll.setWidgetResizable(True)
        self.result_container = QWidget()
        self.result_grid = QGridLayout(self.result_container)
        self.result_grid.setSpacing(20)
        self.result_scroll.setWidget(self.result_container)

        layout.addWidget(self.result_scroll)

        self.stack.addWidget(self.match_ui)

    def run_college_match(self):
        state = self.state_input.text().strip().upper()
        try:
            min_sat = int(self.sat_min_input.text())
            max_sat = int(self.sat_max_input.text())
        except ValueError:
            QMessageBox.warning(self, "Input Error", "Please enter valid numerical SAT scores.")
            return

        field_map = {
            "SAT Score": "latest.admissions.sat_scores.average.overall",
            "Admission Rate": "latest.admissions.admission_rate.overall",
            "Student Size": "latest.student.size",
            "College Name": "school.name"
        }

        sort_field_label = self.sort_field.currentText()
        api_sort_param = field_map.get(sort_field_label) if sort_field_label != "None" else ""
        sort_order = self.sort_order.currentText()

        ownership = self.school_type.currentText()
        ownership_param = ""
        if ownership == "Public":
            ownership_param = "1"
        elif ownership == "Private":
            ownership_param = "2"

        results = fetch_colleges(
            min_sat=min_sat,
            max_sat=max_sat,
            state=state,
            ownership=ownership_param,
            api_key="fIrC5AldgOegvmMhUPhiaV0N7rYu31QkV3pagMsc"
        )

        if sort_field_label != "None":
            sort_key_api_name = field_map.get(sort_field_label)
            if sort_key_api_name:
                reverse = (sort_order == "Descending")

                if sort_key_api_name == "school.name":
                    results = sorted(results, key=lambda r: r.get("school.name", "").lower(), reverse=reverse)
                else:
                    results = sorted(
                        results,
                        key=lambda r: r.get(sort_key_api_name) if r.get(sort_key_api_name) is not None else (
                            float('-inf') if reverse else float('inf')),
                        reverse=reverse
                    )

        if not results:
            QMessageBox.information(self, "No Matches Found",
                                    "No colleges found with the given criteria. "
                                    "Try adjusting your filters or check your internet connection.")
            for i in reversed(range(self.result_grid.count())):
                widget = self.result_grid.itemAt(i).widget()
                if widget:
                    widget.setParent(None)
            return

        for i in reversed(range(self.result_grid.count())):
            widget = self.result_grid.itemAt(i).widget()
            if widget:
                widget.setParent(None)

        row, col = 0, 0
        for i, c in enumerate(results):
            name = c.get("school.name", "N/A")
            city = c.get("school.city", "N/A")
            state = c.get("school.state", "N/A")
            sat = c.get("latest.admissions.sat_scores.average.overall", "N/A")
            size = c.get("latest.student.size", "N/A")
            rate = c.get("latest.admissions.admission_rate.overall")
            admission = f"{round(rate * 100)}%" if rate is not None else "N/A"
            url = c.get("school.school_url", "N/A")

            card = QWidget()
            card.setStyleSheet("background-color: #2b2b2b; border-radius: 10px; padding: 12px;")
            vbox = QVBoxLayout(card)
            vbox.setContentsMargins(10, 10, 10, 10)
            vbox.setSpacing(6)

            name_lbl = QLabel(f"<b>{name}</b>")
            name_lbl.setStyleSheet("color: white; font-size: 15px;")
            loc_lbl = QLabel(f"<i>{city}, {state}</i>")
            loc_lbl.setStyleSheet("color: #ccc; font-size: 12px;")
            stats_lbl = QLabel(f"SAT Avg: <b>{sat}</b> | Students: <b>{size}</b> | Admit Rate: <b>{admission}</b>")
            stats_lbl.setStyleSheet("color: white; font-size: 12px;")

            url_lbl = QLabel(f"<a href='{url}' style='color: #88c0ff; text-decoration: none;'>{url}</a>")
            url_lbl.setOpenExternalLinks(True)
            url_lbl.setStyleSheet("font-size: 12px;")

            vbox.addWidget(name_lbl)
            vbox.addWidget(loc_lbl)
            vbox.addWidget(stats_lbl)
            vbox.addWidget(url_lbl)

            self.result_grid.addWidget(card, row, col)
            col += 1
            if col == 3:
                row += 1
                col = 0

        for _ in range(self.result_grid.columnCount()):
            self.result_grid.setColumnStretch(_, 1)
        self.result_grid.setRowStretch(row + 1, 1)

    def reset_match_inputs(self):
        self.state_input.setText("")
        self.sat_min_input.setText("")
        self.sat_max_input.setText("")

    def build_career_result_ui(self):
        self.career_result_ui = QWidget()
        layout = QVBoxLayout(self.career_result_ui)
        self.back_to_form_btn = QPushButton("← Edit Inputs")
        self.back_to_form_btn.setStyleSheet("padding: 8px; background: #444; color: white; border-radius: 6px;")
        self.back_to_form_btn.clicked.connect(lambda: self.stack.setCurrentWidget(self.career_ui))
        layout.addWidget(self.back_to_form_btn)

        self.header_row = QHBoxLayout()
        self.header_row.setSpacing(15)
        layout.addLayout(self.header_row)

        self.expanded_area = QHBoxLayout()
        self.expanded_area.setSpacing(15)
        layout.addLayout(self.expanded_area)

        layout.addStretch(1)
        self.stack.addWidget(self.career_result_ui)

    def toggle_mode(self):
        if self.current_mode == "career":
            self.switch_to_explainer()
        elif self.current_mode == "explainer":
            self.switch_to_career()
        elif self.current_mode == "college_match":
            self.switch_to_explainer()

    def switch_to_career(self):
        self.current_mode = "career"
        self.sectionLabel.setText("Career Counselor")
        self.stack.setCurrentWidget(self.career_ui)
        self.update_top_bar_buttons()

    def build_career_ui(self):
        if hasattr(self, 'career_ui'):
            return

        self.career_ui = QWidget()
        layout = QVBoxLayout(self.career_ui)
        layout.setContentsMargins(40, 30, 40, 30)
        layout.setSpacing(20)
        self.career_inputs = {}

        labels = ["Skills", "Interests", "Classes Taken", "GPA", "Extracurriculars", "Résumé (Optional)"]
        skills_list = [
            "Python", "Java", "C++", "C#", "JavaScript", "TypeScript", "HTML", "CSS", "SQL", "NoSQL",
            "MATLAB", "R", "Swift", "Kotlin", "Dart", "Go", "Rust", "Bash", "Linux", "Shell Scripting",
            "Git", "GitHub", "Version Control", "Docker", "Kubernetes", "CI/CD", "Cloud Computing",
            "AWS", "Azure", "Google Cloud", "Firebase", "Heroku", "Network Security", "Cybersecurity",
            "Ethical Hacking", "Cryptography", "Penetration Testing", "Computer Vision", "OpenCV",
            "Machine Learning", "Deep Learning", "TensorFlow", "Keras", "PyTorch", "Scikit-learn",
            "NLP", "Chatbot Development", "Prompt Engineering", "Data Science", "Pandas", "NumPy",
            "Data Visualization", "Matplotlib", "Seaborn", "Plotly", "Database Management",
            "MongoDB", "MySQL", "PostgreSQL", "Firebase Realtime DB", "SQLite", "Figma", "Adobe XD",
            "UX/UI Design", "Responsive Design", "Mobile App Development", "React Native", "Flutter",
            "React", "Vue", "Angular", "Svelte", "Node.js", "Express.js", "Next.js", "Back-End Development",
            "API Development", "REST", "GraphQL", "3D Modeling", "Blender", "Fusion 360", "CAD", "TinkerCAD",
            "SolidWorks", "Photoshop", "Illustrator", "Premiere Pro", "After Effects", "Video Editing",
            "Audio Editing", "Audacity", "DaVinci Resolve", "Unity", "Unreal Engine", "Game Design",
            "Physics Simulations", "Arduino", "Raspberry Pi", "Microcontrollers", "Sensors", "IoT",
            "Circuit Design", "Breadboarding", "Soldering", "PCB Design", "Robotics", "ROS",
            "Team Collaboration", "Leadership", "Public Speaking", "Writing", "Critical Thinking",
            "Time Management", "Project Management", "Agile Development", "Scrum", "JIRA", "Trello",
            "Slack", "LaTeX", "Excel", "PowerPoint", "Word", "Notion", "Obsidian", "VS Code", "Terminal",
            "CLI Tools", "Automation", "Regex", "Web Scraping", "BeautifulSoup", "Selenium"
        ]

        classes_list = [
            "AP Calculus AB", "AP Calculus BC", "AP Statistics", "AP Computer Science A",
            "AP Computer Science Principles", "AP Physics 1", "AP Physics 2",
            "AP Physics C: Mechanics", "AP Physics C: Electricity and Magnetism",
            "AP Biology", "AP Chemistry", "AP Environmental Science", "AP Psychology",
            "AP Microeconomics", "AP Macroeconomics", "AP Human Geography", "AP US History",
            "AP World History", "AP European History", "AP Art History", "AP English Language",
            "AP English Literature", "AP French", "AP Spanish", "AP German", "AP Chinese",
            "AP Music Theory", "Honors Algebra 1", "Honors Geometry", "Honors Algebra 2",
            "Honors Pre-Calculus", "Honors Biology", "Honors Chemistry", "Honors Physics",
            "Dual Enrollment Calculus", "Dual Enrollment Statistics", "College Algebra",
            "College Pre-Calculus", "College Chemistry", "College Physics", "College Biology",
            "Intro to Programming", "Advanced Programming", "Data Structures", "Algorithms",
            "Mobile App Development", "Web Development", "Game Design", "3D Modeling", "Animation",
            "Digital Electronics", "Robotics", "Intro to Engineering", "Advanced Robotics",
            "Engineering Design & Development", "Machine Learning", "AI Fundamentals",
            "Embedded Systems", "Mechatronics", "Cybersecurity", "Networking", "Cloud Computing",
            "Graphic Design", "Video Editing", "Journalism", "Creative Writing", "Speech & Debate",
            "Mock Trial", "Film Studies", "Photography", "Studio Art", "Music Production",
            "Financial Literacy", "Personal Finance", "Economics", "Business Law", "Entrepreneurship",
            "Marketing", "Accounting", "Psychology", "Sociology", "Forensics", "Astronomy",
            "Health", "PE", "Drivers Ed", "Career & Technical Education (CTE)", "Work-Based Learning",
            "Research Seminar", "Research Methods", "Capstone Project", "STEM Explorations"
        ]

        activities_list = [
            "FIRST Robotics", "Science Olympiad", "Math Team", "CyberPatriot", "Hack Club",
            "Coding Club", "AI Club", "Machine Learning Club", "Game Dev Club", "Quantum Computing Club",
            "STEM Club", "Tech Ambassadors", "Science Fair", "Intel ISEF", "Regeneron STS", "Hackathons",
            "MIT Blueprint", "NYU Hack", "Major League Hacking", "Model UN", "Mock Trial", "Debate Club",
            "Speech Club", "Drama Club", "Theater", "Musical Theater", "Choir", "Band", "Orchestra",
            "Jazz Ensemble", "Art Club", "Photography Club", "Film Club", "Podcast Club", "Newspaper",
            "Yearbook", "Literary Magazine", "Creative Writing Club", "National Honor Society", "Math Honor Society",
            "Science Honor Society", "Computer Science Honor Society", "Student Government", "Class Council",
            "Key Club", "Red Cross", "Volunteer Club", "Peer Tutoring", "Mentorship Program",
            "School Ambassadors", "Library Intern", "AV Tech Team", "Media Crew", "Coding Volunteer",
            "Hospital Volunteer", "Food Pantry Volunteer", "Environmental Club", "Green Team",
            "Recycling Program", "UNICEF Club", "DECA", "FBLA", "Business Club", "Entrepreneurship Club",
            "Finance Club", "Investment Club", "BSA", "BSU", "ASU", "Hispanic Culture Club",
            "Asian Culture Club", "Muslim Student Association", "Christian Fellowship",
            "GSA", "Cultural Exchange Club", "Chess Club", "Board Game Club",
            "Anime Club", "K-pop",  # THIS IS WHERE IT WAS CUT OFF BEFORE
            "Dance Club", "Yoga Club", "Sports Management Club",
            "Varsity Football", "Varsity Soccer", "Varsity Basketball", "Varsity Volleyball",
            "Varsity Track", "Varsity Cross Country", "Varsity Baseball", "Varsity Softball",
            "Varsity Wrestling", "Varsity Bowling", "Varsity Tennis", "Varsity Golf", "Varsity Swim",
            "Varsity Ski", "Varsity Gymnastics", "Varsity Cheer", "Varsity Robotics", "Esports Team"
        ]

        input_font = QFont("Antipasto Pro DemiBold", 12)
        for label_text in labels:
            lbl = QLabel(label_text)
            lbl.setFont(input_font)
            lbl.setStyleSheet("color: white; margin-bottom: 4px;")
            box = QLineEdit()
            box.setFont(input_font)
            box.setStyleSheet("""
            QLineEdit {
                background: rgba(255, 255, 255, 0.06);
                color: white;
                padding: 10px;
                border-radius: 10px;
                border: 1px solid rgba(255, 255, 255, 0.15);
            }
            QLineEdit:focus {
                border: 1px solid #88c0ff;
            }
            """)
            layout.addWidget(lbl)
            layout.addWidget(box)
            self.career_inputs[label_text.lower()] = box

            completer = None
            if "skills" in label_text.lower():
                completer = QCompleter(self)
                model = QStringListModel()
                model.setStringList(skills_list)
                completer.setModel(model)
            elif "classes" in label_text.lower():
                completer = QCompleter(self)
                model = QStringListModel()
                model.setStringList(classes_list)
                completer.setModel(model)
            elif "extracurricular" in label_text.lower():
                completer = QCompleter(self)
                model = QStringListModel()
                model.setStringList(activities_list)
                completer.setModel(model)

            if completer:
                completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
                completer.setFilterMode(Qt.MatchFlag.MatchContains)
                box.setCompleter(completer)

        self.career_generate = QPushButton("Generate Career Guidance")
        self.career_generate.setFont(QFont("Typo Round Regular Demo", 14))
        self.career_generate.setStyleSheet("""
        QPushButton {
            background: rgba(0, 123, 255, 0.8);
            color: white;
            padding: 12px;
            border-radius: 12px;
            border: 1px solid rgba(255, 255, 255, 0.1);
        }
        QPushButton:hover {
            background: rgba(30, 140, 255, 0.9);
        }
    """)
        self.career_generate.clicked.connect(self.generate_career)
        layout.addWidget(self.career_generate)

        self.loading_gif = QLabel()
        if os.path.exists("loading.gif"):
            self.loading_movie = QMovie("loading.gif")
            self.loading_gif.setMovie(self.loading_movie)
            self.loading_gif.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.loading_gif.setVisible(False)
            layout.addWidget(self.loading_gif)
        else:
            print("WARNING: loading.gif not found. Loading animation will not display.")
            self.loading_movie = None

        layout.addStretch()
        self.stack.addWidget(self.career_ui)

    def generate_career(self):
        data = {k: v.text().strip() for k, v in self.career_inputs.items()}
        prompt = f"""
You are an elite AI career counselor helping a high school student explore future opportunities. Be realistic. No bold words.
Label each section with a number and a period, using these exact section titles:
1. Ideal Career Paths
2. Recommended College Majors
3. Best Universities for Recommended Majors
4. Preparation Roadmap
Do not use any other section headers or formatting.
Be thorough and detailed in your explanations.

Profile:
Skills: {data['skills']}
Interests: {data['interests']}
Classes Taken: {data['classes taken']}
GPA: {data['gpa']}
Extracurriculars: {data['extracurriculars']}
Résumé Info: {data['résumé (optional)'] or 'N/A'}
No bold words or letters, have at least 5 career paths, 5 recommended college majors, 10 universities.
"""
        self.last_profile = data
        self.college_profile = {
            "major": data.get("interests", ""),
            "state": "",
            "min_sat": 1000,
            "max_sat": 1600
        }

        if self.loading_movie:
            self.loading_gif.setVisible(True)
            self.loading_movie.start()

        def worker():
            try:
                # Ensure model is accessible and configured in this thread's context if needed
                # This is a global model, so it should be fine if configured once.
                if model is None:
                    # Handle case where model wasn't initialized due to missing API key
                    QMetaObject.invokeMethod(self, "show_results", Qt.ConnectionType.QueuedConnection,
                                             Q_ARG(str, "ERROR: Gemini model not initialized. API key missing."))
                    return

                response = model.generate_content(prompt)
                result = response.text
            except Exception as e:
                result = f"ERROR: {e}"
            self.history.append({"profile": data, "result": result})
            self.save_history()
            QMetaObject.invokeMethod(self, "show_results", Qt.ConnectionType.QueuedConnection, Q_ARG(str, result))

        threading.Thread(target=worker).start()

    @pyqtSlot(str)
    def show_results(self, text):
        if self.loading_movie:
            self.loading_movie.stop()
            self.loading_gif.setVisible(False)

        if text.startswith("ERROR:"):
            QMessageBox.critical(self, "Gemini API Error", text)
            return

        for layout in [self.header_row, self.expanded_area]:
            for i in reversed(range(layout.count())):
                widget = layout.itemAt(i).widget()
                if widget:
                    widget.setParent(None)

        def extract_sections(text):
            pattern = r"^(\d\.\s+[^.]+?\s*\.)"
            matches = list(re.finditer(pattern, text, re.MULTILINE))
            sections = {}
            for i, match in enumerate(matches):
                start = match.end()
                end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
                header = match.group(1).strip()
                title = re.sub(r"^\d\.\s+", "", header).rstrip(" .")
                content = text[start:end].strip()
                sections[title] = content
            return sections

        sections = extract_sections(text)
        if not sections:
            QMessageBox.information(self, "Gemini Output",
                                    "The AI did not return content in the expected format. Please try again.")
            return

        self.expanded_cards = {}

        for title, content in sections.items():
            content = content.replace("* **", "     • ").replace("-", "       • ")
            summary = content.split(".")[0] + "." if "." in content else content.split("\n")[0]
            header = CardHeader(title, summary)
            header.clicked.connect(lambda t=title, c=content: self.toggle_expanded_card(t, c))
            self.header_row.addWidget(header)
        self.stack.setCurrentWidget(self.career_result_ui)

    def toggle_expanded_card(self, title, content):

        for i in range(self.expanded_area.count()):
            widget = self.expanded_area.itemAt(i).widget()
            if isinstance(widget, ExpandedCard) and widget.title == title:
                widget.setParent(None)
                return

        open_cards = [self.expanded_area.itemAt(i).widget() for i in range(self.expanded_area.count()) if
                      isinstance(self.expanded_area.itemAt(i).widget(), ExpandedCard)]
        if len(open_cards) >= 2:
            open_cards[0].setParent(None)

        card = ExpandedCard(title, content, font=QFont("Typo Round Regular Demo"))
        self.expanded_area.addWidget(card)

    def save_history(self):
        with open("career_history.json", "w") as f:
            json.dump(self.history, f, indent=2)

    def load_history(self):
        if os.path.exists("career_history.json"):
            try:
                with open("career_history.json", "r") as f:
                    return json.load(f)
            except json.JSONDecodeError:
                print("Error loading career_history.json, starting fresh.")
                return []
        return []

    def switch_to_explainer(self):
        self.current_mode = "explainer"
        self.sectionLabel.setText("Academic Explainer")
        self.stack.setCurrentWidget(self.explainer_ui)
        self.update_top_bar_buttons()

    def build_explainer_ui(self):
        if hasattr(self, 'explainer_ui'):
            return

        self.explainer_ui = QWidget()
        layout = QVBoxLayout(self.explainer_ui)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.historyList = QListWidget()
        self.historyList.setMaximumWidth(250)
        self.splitter.addWidget(self.historyList)
        self.historyList.itemClicked.connect(self.on_select_history)

        for topic in self.explainer_data.get("history", []):
            self.historyList.addItem(QListWidgetItem(topic))

        right = QWidget()
        rlayout = QVBoxLayout(right)

        top = QHBoxLayout()
        self.topicInput = QLineEdit()
        self.generateBtn = QPushButton("Generate")
        self.testModeBtn = QPushButton("Test Mode")
        self.newChatBtn = QPushButton("New Chat")
        self.themeSwitchBtn = QPushButton("🌓")

        top.addWidget(self.topicInput)
        top.addWidget(self.generateBtn)
        top.addWidget(self.testModeBtn)
        top.addWidget(self.newChatBtn)
        top.addWidget(self.themeSwitchBtn)
        rlayout.addLayout(top)

        self.explanationDisplay = QTextEdit()
        self.explanationDisplay.setReadOnly(True)
        rlayout.addWidget(self.explanationDisplay, 4)

        self.notesArea = QTextEdit()
        self.notesArea.setPlaceholderText("Your notes...")
        rlayout.addWidget(self.notesArea, 1)
        self.notesArea.textChanged.connect(self.on_notes_changed)

        self.status = QLabel("Ready")
        self.status.setAlignment(Qt.AlignmentFlag.AlignRight)
        rlayout.addWidget(self.status)

        self.splitter.addWidget(right)
        layout.addWidget(self.splitter)
        self.explainer_response_ready.connect(self.handle_explainer_response)
        self.generateBtn.clicked.connect(self.on_generate)
        self.testModeBtn.clicked.connect(lambda: self.on_generate(test_mode=True))
        self.newChatBtn.clicked.connect(self.on_new_chat)
        self.topicInput.returnPressed.connect(self.on_generate)
        self.themeSwitchBtn.clicked.connect(self.toggle_theme)

        self.stack.addWidget(self.explainer_ui)
        self.apply_theme(self.current_theme)

        ### REPLACE the entire `on_generate` method WITH THIS ###
    def on_generate(self, test_mode=False):
        topic = self.topicInput.text().strip()
        if not topic:
            QMessageBox.warning(self, "Input Required", "Please enter a topic to explain.")
            return

        self.status.setText("Generating...")
        self.explanationDisplay.clear()

        def worker():
            try:
                prompt = (
                    f"Imagine you're Richard Feynman explaining '{topic}' to curious high schoolers. "
                    "Go deep with examples and metaphors. No bold text or lists — just smooth, connected teaching. Be thorough and don't write it like a script, write it like an explanation by richard feynman. Don't address the reader as class. Also, be thorough, extremely thorough. Long. Don't gloss over things, go deep. Ten pages at least."
                )
                if test_mode:
                    prompt += "\nAlso prepare the student for a test with formulas, edge cases, and pitfalls. Don't label the sections be natural. Should include them still though. Still thorough through everything"

                if model is None:
                    raise ValueError("Gemini model not initialized. API key missing.")

                response = model.generate_content(prompt)

                if not response.parts:
                    feedback = response.prompt_feedback
                    block_reason = "Unknown"
                    if feedback and hasattr(feedback, 'block_reason') and feedback.block_reason:
                        block_reason = feedback.block_reason.name
                    raise ValueError(f"The AI response was empty. Reason: {block_reason}")

                result = response.text.strip()
                # Emit a success signal with the results
                self.explainer_response_ready.emit({
                    "status": "success",
                        "topic": topic,
                        "result": result
                })

            except Exception as e:
                # Emit an error signal with the error details
                self.explainer_response_ready.emit({
                        "status": "error",
                        "topic": topic,
                        "error": str(e)
                 })

        threading.Thread(target=worker, daemon=True).start()


    @pyqtSlot(dict)
    def handle_explainer_response(self, response_data):
        """
            Safely updates the Academic Explainer UI from the main thread.
        """
        status = response_data.get("status")
        topic = response_data.get("topic")

        if status == "success":
            result = response_data.get("result", "")
            self.explanationDisplay.setText(result)
            self.status.setText("Done.")

                # Update history and save data
            if topic not in self.explainer_data.get("history", []):
                self.explainer_data.setdefault("history", []).append(topic)
                self.historyList.addItem(topic)

            self.explainer_data.setdefault("topics", {}).setdefault(topic, {})["explanation"] = result
            notes = self.explainer_data.get("topics", {}).get(topic, {}).get("notes", "")
            self.notesArea.setText(notes)
            self.save_explainer_data()

        elif status == "error":
            error_message = response_data.get("error", "An unknown error occurred.")
            self.status.setText(f"Error.")
            QMessageBox.critical(self, "Gemini API Error", f"Failed to generate explanation:\n{error_message}")
    def on_new_chat(self):
        topic = self.topicInput.text().strip()
        if topic:
            if topic not in self.explainer_data.get("history", []):
                self.explainer_data.setdefault("history", []).append(topic)
                self.historyList.addItem(topic)
            self.explainer_data.setdefault("topics", {}).setdefault(topic, {})[
                "explanation"] = self.explanationDisplay.toPlainText()
            self.explainer_data.get("topics", {}).get(topic, {})["notes"] = self.notesArea.toPlainText()
            self.save_explainer_data()
            self.topicInput.clear()
            self.explanationDisplay.clear()
            self.notesArea.clear()
            self.status.setText("New chat started.")
        else:
            QMessageBox.information(self, "New Chat", "Current topic input is empty. Starting a fresh session.")
            self.topicInput.clear()
            self.explanationDisplay.clear()
            self.notesArea.clear()
            self.status.setText("New chat started.")

    def on_select_history(self, item):
        topic = item.text()
        self.topicInput.setText(topic)
        self.explanationDisplay.setText(self.explainer_data.get("topics", {}).get(topic, {}).get("explanation", ""))
        self.notesArea.blockSignals(True)
        self.notesArea.setText(self.explainer_data.get("topics", {}).get(topic, {}).get("notes", ""))
        self.notesArea.blockSignals(False)

    def on_notes_changed(self):
        topic = self.topicInput.text().strip()
        if topic:
            self.explainer_data.setdefault("topics", {}).setdefault(topic, {})["notes"] = self.notesArea.toPlainText()
            self.save_explainer_data()

    def toggle_theme(self):
        self.current_theme = "hand" if self.current_theme == "dark" else "dark"
        with open("settings.json", "w") as f:
            json.dump({"theme": self.current_theme}, f)
        self.apply_theme(self.current_theme)

    def apply_theme(self, theme):
        base_dark_style = """
            QMainWindow, QWidget { background: #121212; color: #EAEAEA; }
            QTextEdit, QLineEdit, QComboBox, QDateEdit { 
                background: #1c1c1c; color: #EAEAEA; border-radius: 6px; 
                border: 1px solid #444; padding: 8px; font-size: 18px; 
            }
            QPushButton { 
                background: #333; border: 1px solid #444; color: #EEE; 
                padding: 6px; border-radius: 6px; 
            }
            QPushButton:hover { background: #444; }
            QLabel { color: #EAEAEA; }
            QListWidget { background: #1c1c1c; border: 1px solid #444; color: #EAEAEA; }
            QListWidget::item:selected { background: #3a3a3a; color: white; }
        """

        base_hand_style = """
            QMainWindow, QWidget { background: #FAF9F6; color: #222; }
            QTextEdit, QLineEdit, QComboBox, QDateEdit { 
                background: #FCF9E6; color: #000000; border: 2px dashed #333; 
                padding: 8px; font-size: 24px; 
            }
            QPushButton { 
                background: #FFFBE6; border: 1px dashed #444; padding: 6px; }
            QPushButton:hover { background: #FFFFCC; }
            QLabel { color: #222; }
            QListWidget { background: #FCF9E6; border: 2px dashed #444; color: #222; }
            QListWidget::item:selected { background: #F0EAD6; color: #000; }
        """

        if theme == "dark":
            font = QFont("Typo Round Regular Demo", 12)
            QApplication.setFont(font)
            self.setStyleSheet(base_dark_style)
        else:  # "hand" theme
            font = QFont("Black-Brownies", 16)
            QApplication.setFont(font)
            self.setStyleSheet(base_hand_style)

        self.update_top_bar_buttons()

        if hasattr(self, 'app_entry_panel'):
            self.app_entry_panel.apply_styles()
            self.app_dashboard_panel.apply_styles()

    def load_explainer_data(self):
        if os.path.exists("history.json"):
            try:
                with open("history.json", "r") as f:
                    all_data = json.load(f)
                    return all_data
            except json.JSONDecodeError:
                print("Error reading history.json, starting fresh for explainer.")
                return {"topics": {}, "history": []}
        return {"topics": {}, "history": []}

    def save_explainer_data(self):
        with open("history.json", "w") as f:
            json.dump(self.explainer_data, f, indent=2)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(18, 18, 18))
    palette.setColor(QPalette.ColorRole.Text, Qt.GlobalColor.white)
    app.setPalette(palette)

    win = CombinedApp()
    win.show()
    sys.exit(app.exec())