import datetime
import secrets
import smtplib
import ssl
import string
import threading
from email.message import EmailMessage
from typing import Dict, Tuple

EMAIL_SENDER = "cyber.emails.sender@gmail.com"
EMAIL_PASSWORD = "ujfukfdmymyviljv"

EMAIL_SUBJECT = "Your Verification Code"
EMAIL_BODY = "Hi NAME, Your Verification code is: CODE.\nThe code will expire in 5 minutes."

CODE_EXPIRATION = datetime.timedelta(seconds=300)
CODES_LOCK = threading.Lock()

codes_tracker: Dict[str, Tuple[str, datetime.datetime]] = dict()


def send_code(username: str, email: str):
    code = get_code()
    code_expiration = datetime.datetime.now() + CODE_EXPIRATION
    with CODES_LOCK:
        codes_tracker[code] = (username, code_expiration)
    send_email(email, EMAIL_SUBJECT,
               EMAIL_BODY.replace("CODE", code).replace("NAME", username))


def send_email(email_receiver: str, email_subject: str, email_body: str):
    email = EmailMessage()
    email["From"] = EMAIL_SENDER
    email["To"] = email_receiver
    email["Subject"] = email_subject
    email.set_content(email_body)

    context = ssl._create_unverified_context()

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as smtp:
        smtp.login(EMAIL_SENDER, EMAIL_PASSWORD)
        smtp.sendmail(EMAIL_SENDER, email_receiver, email.as_string())


def verify_code(username: str, code: str) -> bool:
    with CODES_LOCK:
        if code in codes_tracker:
            return codes_tracker[code][0] == username and codes_tracker[code][1] > datetime.datetime.now()
        return False


def get_code():
    alphabet = string.ascii_letters + string.digits
    while True:
        code = ''.join(secrets.choice(alphabet) for i in range(10))
        if (any(c.islower() for c in code)
                and any(c.isupper() for c in code)
                and sum(c.isdigit() for c in code) >= 3):
            with CODES_LOCK:
                if code in codes_tracker:
                    if codes_tracker[code][1] >= datetime.datetime.now() + CODE_EXPIRATION:
                        codes_tracker.pop(code)
                    else:
                        continue
            return code
