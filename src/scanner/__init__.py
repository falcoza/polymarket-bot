"""Smart money scanner module for detecting whale activity."""

from .models import WhaleTrade, WhaleAlert, WalletProfile, AlertType
from .activity_client import ActivityClient
from .whale_detector import WhaleDetector

__all__ = [
    "WhaleTrade",
    "WhaleAlert",
    "WalletProfile",
    "AlertType",
    "ActivityClient",
    "WhaleDetector",
]
