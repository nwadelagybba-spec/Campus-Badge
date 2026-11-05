"""Custom exceptions for CampusBadge.

Every exception carries a short, friendly `message` that is safe to show to
users. Technical details are logged separately and never shown on the website.
"""


class CampusBadgeError(Exception):
    default_message = "Something went wrong. Please try again."

    def __init__(self, message=None):
        self.message = message or self.default_message
        super().__init__(self.message)


class ValidationError(CampusBadgeError):
    default_message = "Please check the information you entered."


class AuthenticationError(CampusBadgeError):
    default_message = "Invalid email or password."


class DuplicateAccountError(CampusBadgeError):
    default_message = "An account with that email already exists."


class InvalidQRCodeError(CampusBadgeError):
    default_message = "This QR code is invalid or has expired."


class DuplicateTimeInError(CampusBadgeError):
    default_message = "You have already timed in for this event."


class MissingTimeInError(CampusBadgeError):
    default_message = "You need to time in before you can time out."


class DuplicateTimeOutError(CampusBadgeError):
    default_message = "You have already timed out for this event."


class UnauthorizedAccessError(CampusBadgeError):
    default_message = "You do not have permission to do that."


class EventFullError(CampusBadgeError):
    default_message = "Sorry, this event is already full."


class DatabaseError(CampusBadgeError):
    default_message = "A database problem occurred. Please try again in a moment."


class FileProcessingError(CampusBadgeError):
    default_message = "A file problem occurred. Please try again or contact an administrator."
