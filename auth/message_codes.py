class CommandCodes:
    SignIn = b'\x01'
    SignUp = b'\x02'
    SendCode = b'\x03'
    VerifyCode = b'\x04'
    ResetPassword = b'\x05'
    SetKeyExchangeMode = b'\x06'
    GetServerStatus = b'\x07'


class ResponseCodes:
    SignInSuccess = b'\x11'
    SignUpSuccess = b'\x12'
    TakenUsername = b'\x13'
    SignInFailed = b'\x14'
    GeneralError = b'\x15'
    CodeSent = b'\x16'
    VerificationSuccess = b'\x17'
    VerificationFailed = b'\x18'
    ResetPasswordSuccess = b'\x19'
    WrongUsername = b'\x1A'
    AdminLoginSuccess = b'\x1B'
    ServerStatus = b'\x1C'


class KeyExchangeMode:
    RSA = b'\x21'
    DH = b'\x22'
    RSAPublicKey = b'\x23'
