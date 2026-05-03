'''Live Activity Panel for Cockpit

Placeholder implementation for SAL-3636. Provides `LiveActivityPanel` class that
stores activity records (mocked) and formats them for UI consumption. Real
integration with SSE/polling to be added later.
'''

from typing import List, Dict
import datetime

class LiveActivityPanel:
    """Data source for the live activity panel.

    Each activity dict should contain:
    - ``session_id`` (string)
    - ``current_task`` (string)
    - ``last_heartbeat`` (ISO timestamp string)
    - ``node_id`` (string)
    - ``harness`` (string)
    """

    def __init__(self) -> None:
        # In production this would set up SSE subscriptions or polling.
        self._activities: List[Dict] = []

    def fetch_activities(self) -> List[Dict]:
        """Return the raw list of activity dicts.

        Currently returns the internal list, which is empty by default to
        satisfy the empty‑state requirement.
        """
        return self._activities

    def update(self) -> None:
        """Placeholder for periodic update logic (poll/SSE).

        No‑op for now.
        """
        pass

    @staticmethod
    def _format_row(raw: Dict) -> Dict:
        """Format a raw activity dict for UI display.

        Truncates ``session_id`` to 8 characters and computes ``age`` as
        seconds since ``last_heartbeat``.
        """
        now = datetime.datetime.utcnow()
        last_hb_str = raw.get("last_heartbeat", now.isoformat())
        try:
            last_hb = datetime.datetime.fromisoformat(last_hb_str)
        except ValueError:
            last_hb = now
        age_seconds = int((now - last_hb).total_seconds())
        return {
            "session_id": str(raw.get("session_id", ""))[:8],
            "current_task": raw.get("current_task", ""),
            "age": age_seconds,
            "last_heartbeat": last_hb_str,
            "node_id": raw.get("node_id", ""),
            "harness": raw.get("harness", ""),
        }

    def get_formatted_activities(self) -> List[Dict]:
        """Return activities formatted for the UI.
        """
        return [self._format_row(a) for a in self._activities]
