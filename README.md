# Discord Archive

A local Windows app to export Discord channels and browse saved messages.

## Quick start

1. Install Python 3.10 or newer from https://www.python.org/downloads/.
   During installation, enable **Add Python to PATH**.
2. Extract this entire ZIP to a folder on your computer.
3. Double-click **Start.cmd**. The app opens in your browser.
4. Enter your own Discord token, select servers/channels, and click **Start export**.

No extra Python packages are needed. Keep the app's terminal open while exporting.
If the browser does not open, copy the local link shown in the terminal.

## Stop the app

Double-click **Stop.cmd**, or use **Settings > Quit app**.
Closing only the browser tab does not stop the app. Progress is saved for resume.

## Features

- Select multiple channels or whole servers, including accessible threads.
- Filter by dates, text, authors, or pinned messages.
- Export Markdown, HTML, JSON, JSONL, text, or CSV.
- Pause/resume jobs, search saved messages, and download results as a ZIP.
- Optional attachment downloads and light/dark themes.

## Your data

The app creates **data/** for its local library and **exports/** for output files.
This package contains no previous exports, databases, logs, or saved credentials.
Your token stays in memory and is cleared when you disconnect or quit.
Never share your token. Exported files are not encrypted.

Only accessible messages can be exported. Deleted messages cannot be recovered.
Check the channel report for missing permissions or incomplete results.

Discord prohibits personal-account automation and may terminate accounts that use
it. A bot account is the supported option where it has channel access, Read Message
History permission, and Message Content Intent enabled.

https://support.discord.com/hc/en-us/articles/115002192352-Automated-User-Accounts-Self-Bots
