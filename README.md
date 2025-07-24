Pathwise - Your Personal AI Career & Academic Co-Pilot 🚀
What is this?
Pathwise is a desktop application I built to be a co-pilot for high school students navigating the chaotic journey of college applications and academic planning. It's not just another boring spreadsheet; it's a multi-tool powered by the Google Gemini AI to give you personalized career guidance, break down complex academic topics, and keep your college applications organized.

This project was born from my own experience as a student trying to connect my passions in quantum physics, engineering, and AI with a clear path forward. Pathwise is my attempt to build the tool I wish I had.

Core Features ✨
🧠 AI Career Counselor: Feed it your skills, interests, classes, and extracurriculars, and get detailed, realistic recommendations for career paths, college majors, and top universities.

👨‍🏫 Academic Explainer: Stuck on a tough concept from AP Physics or Calculus? Ask the explainer to break it down for you like a world-class teacher, with a special "test mode" to get you ready for exams.

🎓 College Match Engine: A powerful tool to filter and find colleges based on SAT scores, location, and more, using data from the College Scorecard API.

📬 Application Tracker: A sleek dashboard to manage all your college applications. It includes an optional, secure Gmail integration that automatically scans your inbox for decision emails and updates your application status for you.

Tech Stack 🛠️
Language: Python 3.11+

Framework: PyQt6 for the desktop GUI

AI: Google Gemini API (gemini-1.5-flash)

APIs: Google Gmail API, College Scorecard API

Styling: Custom stylesheets for a modern, responsive UI

🚀 Getting Started: Lock In
Follow these steps to get Pathwise running on your local machine.

1. Prerequisites
Make sure you have Python 3.11 or newer installed on your system.

2. Clone the Repository
Open your terminal or command prompt and clone this repository:

git clone [https://github.com/your-username/pathwise-app.git](https://github.com/your-username/pathwise-app.git)
cd pathwise-app

3. Set Up a Virtual Environment
It's highly recommended to use a virtual environment to keep dependencies clean.

# For Windows
python -m venv venv
venv\Scripts\activate

# For macOS/Linux
python3 -m venv venv
source venv/bin/activate

4. Install Dependencies
Install all the required Python packages using the requirements.txt file.

pip install -r requirements.txt

5. Configure Environment Variables
This is the most important step. You need to provide your own API keys for the app to function.

Find the .env.example file in the project folder.

Make a copy of it and rename the copy to .env.

Open the new .env file. You will fill in the values in the next steps.

🔑 API Key & Credentials Setup
You need to get two things from Google: a Gemini API Key and Gmail API Credentials.

Part A: Get Your Google Gemini API Key
This key allows the AI features of the app to work.

Go to Google AI Studio.

Sign in with your Google account.

Click the "Get API key" button, usually found in the top right corner.

In the menu that appears, click "Create API key in new project".

A new key will be generated for you. Copy this key.

Open your .env file and paste the key:

GEMINI_API_KEY="your-gemini-api-key-goes-here"

Part B: Get Your Gmail API Credentials (credentials.json)
This allows the "Auto Monitor" feature to securely read your emails. The app only gets permission to read emails, not send or delete them.

Go to the Google Cloud Console.

Create a New Project:

Click the project dropdown at the top of the page and click "New Project".

Give it a name like "Pathwise Gmail Client" and click "Create".

Enable the Gmail API:

In the search bar at the top, type "Gmail API" and select it.

Click the "Enable" button.

Configure the OAuth Consent Screen:

In the left-hand menu, go to APIs & Services > OAuth consent screen.

Choose "External" for the User Type and click "Create".

Fill in the required fields:

App name: Pathwise Desktop App

User support email: Your email address.

Developer contact information: Your email address.

Click "Save and Continue" through the "Scopes" and "Test users" sections. You don't need to add anything here. Finally, click "Back to Dashboard".

Create OAuth 2.0 Credentials:

In the left-hand menu, go to APIs & Services > Credentials.

Click "+ CREATE CREDENTIALS" at the top and select "OAuth client ID".

For the Application type, select "Desktop app".

Give it a name (e.g., "Pathwise Desktop Client") and click "Create".

Download Your Credentials:

A window will pop up showing your Client ID and Secret. Close this window.

In the list of "OAuth 2.0 Client IDs," find the one you just created and click the download icon (a down arrow) on the right.

This will download a JSON file. Rename this file to credentials.json.

Move the credentials.json file into the root folder of your Pathwise project (the same folder where Pathwise.py is).

Update Your .env file:

Make sure the path in your .env file points to this file.

GMAIL_CREDENTIALS_PATH="credentials.json"

▶️ Running the Application
Once your dependencies are installed and your .env file is configured, you're ready to go.

Open a terminal in the project folder and run:

python Pathwise.py

The application window should appear. The first time you use a feature that requires Gmail, a browser window will open asking you to grant permission. This is the secure OAuth flow in action!

License
This project is licensed under the MIT License. See the LICENSE file for details.