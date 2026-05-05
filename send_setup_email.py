"""Resend the setup link to bryan's email so he can claim the account."""
import sqlite3, os
from dotenv import load_dotenv
load_dotenv()
from email_helper import send_email

c = sqlite3.connect("tasks.db")
token = c.execute(
    "SELECT token FROM password_reset_tokens WHERE used_at IS NULL ORDER BY id DESC LIMIT 1"
).fetchone()[0]
base = os.environ.get("APP_BASE_URL", "http://localhost:5050").rstrip("/")
link = f"{base}/reset-password/{token}"
print("Setup link:", link)

res = send_email(
    to="powertheworldwithbryan@gmail.com",
    subject="Task Tracker — set your password",
    html=(
        f"<h2>Welcome, Bryan!</h2>"
        f"<p>Your Task Tracker account is ready. Click the link below to set your password:</p>"
        f"<p><a href='{link}'>{link}</a></p>"
        f"<p>This link expires in 24 hours.</p>"
    ),
    text=f"Set your Task Tracker password: {link}",
)
print(res)
