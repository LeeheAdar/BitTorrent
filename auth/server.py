import logging
import socket
import threading
from typing import Any, Callable, Dict, List, Tuple

import auth.database as database
import auth.email_sender as email_sender
import auth.key_exchanging as key_exchanging
import auth.message_codes as message_codes
from auth.encrypted_socket import EncryptedSocket
from auth.key_exchanging import perform_rsa, perform_dh

SERVER_IP = "0.0.0.0"
SERVER_PORT = 12345

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "password1"

active_clients = {}
active_clients_lock = threading.Lock()

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)


def handle_command(func: Callable[[List[str]], Any]) -> Callable:
    def wrapper(entries: List[str]):
        try:
            logging.info(f"Processing command for user: {entries[0]}")
            return func(entries)
        except Exception as e:
            logging.error(f"Error during command processing: {e}")
            return message_codes.ResponseCodes.GeneralError

    return wrapper


@handle_command
def handle_sign_up(entries: List[str]):
    if not database.add_user(entries[0], entries[1], entries[2]):
        logging.warning(f"Username {entries[0]} is already taken")
        return message_codes.ResponseCodes.TakenUsername
    logging.info(f"User {entries[0]} signed up successfully")
    return message_codes.ResponseCodes.SignUpSuccess


@handle_command
def handle_sign_in(entries: List[str]):
    username = entries[0]
    password = entries[1]

    if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        logging.info("Administrator logged in")
        return message_codes.ResponseCodes.AdminLoginSuccess

    if database.valid_entries(username, password):
        logging.info(f"User {username} signed in successfully")
        return message_codes.ResponseCodes.SignInSuccess

    logging.warning(f"Failed sign in for user {username}")
    return message_codes.ResponseCodes.SignInFailed


@handle_command
def handle_send_code(entries: List[str]):
    if database.username_exists(entries[0]):
        email_sender.send_code(entries[0], database.get_email_by_username(entries[0]))
        logging.info(f"Verification code sent to {entries[0]}.")
        return message_codes.ResponseCodes.CodeSent
    logging.warning(f"Username {entries[0]} not found.")
    return message_codes.ResponseCodes.WrongUsername


@handle_command
def handle_verify_code(entries: List[str]):
    if email_sender.verify_code(entries[0], entries[1]):
        logging.info(f"Verification succeeded for {entries[0]}.")
        return message_codes.ResponseCodes.VerificationSuccess
    logging.warning(f"Verification failed for {entries[0]}.")
    return message_codes.ResponseCodes.VerificationFailed


@handle_command
def handle_reset_password(entries: List[str]):
    database.reset_user_password(entries[0], entries[1])
    logging.info(f"Password reset for {entries[0]}.")
    return message_codes.ResponseCodes.ResetPasswordSuccess


@handle_command
def handle_get_server_status(entries: List[str]):
    connected = []

    with active_clients_lock:
        for username, info in active_clients.items():
            connected.append(f"{username}|{info['ip']}|{info['port']}")

    return message_codes.ResponseCodes.ServerStatus, connected


command_handlers: Dict[Any, Callable] = {
    message_codes.CommandCodes.SignUp: handle_sign_up,
    message_codes.CommandCodes.SignIn: handle_sign_in,
    message_codes.CommandCodes.SendCode: handle_send_code,
    message_codes.CommandCodes.VerifyCode: handle_verify_code,
    message_codes.CommandCodes.ResetPassword: handle_reset_password,
    message_codes.CommandCodes.GetServerStatus: handle_get_server_status,
}


def handle_client(sock: socket.socket, client_addr: Tuple[str, int]):
    logged_username = None
    encrypted_socket: EncryptedSocket = None

    try:
        logging.info(f"Handling client {client_addr[0]}:{client_addr[1]}")
        key_exchanging_mode = key_exchanging.recv_data(sock)

        if key_exchanging_mode == message_codes.KeyExchangeMode.RSA:
            symmetric_key = perform_rsa(sock, False)
        else:
            symmetric_key = perform_dh(sock, False)

        encrypted_socket = EncryptedSocket(sock, symmetric_key)

        while True:
            message = encrypted_socket.read_message()

            if not message:
                logging.info(f"Client {client_addr[0]}:{client_addr[1]} has disconnected")
                break

            command, entries = encrypted_socket.parse_message(message)

            logging.info(f"Received command: {command}, Username: {entries[0]}")

            if command == message_codes.CommandCodes.GetServerStatus:
                if logged_username != ADMIN_USERNAME:
                    encrypted_socket.send_message(message_codes.ResponseCodes.GeneralError)
                    continue

            if command in command_handlers:
                result = command_handlers[command](entries)
            else:
                logging.error(f"Unknown command received: {command}")
                result = message_codes.ResponseCodes.GeneralError

            if isinstance(result, tuple):
                code, payload = result
                encrypted_socket.send_message(code, payload)
            else:
                encrypted_socket.send_message(result)

            logging.info(f"Sent response code: {result}")

            if result in (
                    message_codes.ResponseCodes.SignInSuccess,
                    message_codes.ResponseCodes.AdminLoginSuccess
            ):
                logged_username = entries[0]
                with active_clients_lock:
                    active_clients[logged_username] = {
                        "ip": client_addr[0],
                        "port": client_addr[1]
                    }

    except Exception as e:
        logging.error(f"Error handling client {client_addr[0]}:{client_addr[1]} - {e}")

    finally:
        if logged_username:
            with active_clients_lock:
                active_clients.pop(logged_username, None)
        sock.close()


def main():
    try:
        srv_sock = socket.socket()
        srv_sock.bind((SERVER_IP, SERVER_PORT))
        srv_sock.listen()

        database.load_users()

        logging.info(f"Server is now listening on {SERVER_IP}:{SERVER_PORT}")

        client_threads = []

        while True:
            try:
                client_sock, client_addr = srv_sock.accept()
                logging.info(f"New connection from {client_addr[0]}:{client_addr[1]}")

                thread = threading.Thread(
                    target=handle_client,
                    args=(client_sock, client_addr,)
                )
                thread.start()
                client_threads.append(thread)

            except (KeyboardInterrupt, ConnectionError):
                logging.info("Server shutting down...")
                for thread in client_threads:
                    thread.join()
                srv_sock.close()
                break

    except Exception as e:
        logging.error(f"Server encountered an error: {e}")


if __name__ == '__main__':
    main()
