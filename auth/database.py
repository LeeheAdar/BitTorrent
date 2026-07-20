import os
import pickle
import secrets
import threading
from dataclasses import dataclass
from hashlib import sha256
from typing import Dict

USERS_PATH = "users.pkl"
PEPPER_PATH = "pepper"
LOCK = threading.Lock()
PEPPER = "ccc0a8d587380044876b65c1c65504be"


@dataclass
class User:
    username: str
    email: str
    password: str
    salt: str


users_dict: Dict[str, User] = dict()


def load_users():
    global users_dict
    if os.path.exists(USERS_PATH):
        try:
            with LOCK:
                with open(USERS_PATH, "rb") as file:
                    users_dict = pickle.load(file)
        except (pickle.UnpicklingError, EOFError):
            print("Error loading users data. Starting with no registered users.")
    else:
        print("User file not found. Creating a new one...")
        with LOCK:
            with open(USERS_PATH, "wb") as file:
                pickle.dump(users_dict, file)


def save_users():
    with LOCK:
        with open(USERS_PATH, "wb") as file:
            pickle.dump(users_dict, file)


def valid_entries(username: str, password: str) -> bool:
    with LOCK:
        if username in users_dict:
            user = users_dict[username]
            return get_hash_password(password + user.salt + PEPPER) == user.password


def username_exists(username: str) -> bool:
    with LOCK:
        return username in users_dict


def get_hash_password(password: str) -> str:
    m = sha256()
    m.update(password.encode())
    return m.hexdigest()


def add_user(username: str, email: str, password: str) -> bool:
    if username in users_dict:
        return False

    with LOCK:
        user = User(
            username=username,
            email=email,
            password=password,
            salt=secrets.token_hex(16)
        )
        user.password = get_hash_password(password + user.salt + PEPPER)
        users_dict[username] = user
    save_users()
    return True


def reset_user_password(username: str, new_password: str):
    with LOCK:
        users_dict[username].password = get_hash_password(
            new_password + users_dict[username].salt + PEPPER)
    save_users()


def get_email_by_username(username: str) -> str:
    with LOCK:
        return users_dict[username].email
