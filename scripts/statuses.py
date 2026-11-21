"""
statuses.py - The track lifecycle, defined once.

    resolving -> queued -> fetching_info -> downloading -> analyzing -> needs_review -> tagging -> (removed)

  - Any step from resolving through analyzing can fail to "error".
  - Retrying an error goes back to "queued" (or "resolving" if the URL was
    never resolved).
  - If tagging fails, the track goes back to "needs_review" with the error
    message, so the user can fix the metadata and confirm again.

A playlist placeholder in "resolving" is replaced by one "queued" track per
entry rather than transitioning itself. Confirmed tracks are removed, so
there's no "complete" status.
"""

RESOLVING = "resolving"
QUEUED = "queued"
FETCHING_INFO = "fetching_info"
DOWNLOADING = "downloading"
ANALYZING = "analyzing"
NEEDS_REVIEW = "needs_review"
TAGGING = "tagging"
ERROR = "error"

ALL = (RESOLVING, QUEUED, FETCHING_INFO, DOWNLOADING, ANALYZING, NEEDS_REVIEW, TAGGING, ERROR)

# Waiting for the background worker, in pick-up priority order.
PENDING = (RESOLVING, QUEUED)

# A thread is doing I/O for the track right now.
WORKING = (FETCHING_INFO, DOWNLOADING, ANALYZING, TAGGING)

# Can't delete these: a worker owns them. "resolving" is included because the
# worker may be mid-resolve, and resolving a playlist replaces the card.
NOT_DELETABLE = (RESOLVING,) + WORKING

# The UI keeps polling while any track is in one of these.
IN_PROGRESS = PENDING + WORKING

TRANSITIONS = {
    RESOLVING: {QUEUED, ERROR},
    QUEUED: {FETCHING_INFO, ERROR},
    FETCHING_INFO: {DOWNLOADING, ERROR},
    DOWNLOADING: {ANALYZING, ERROR},
    ANALYZING: {NEEDS_REVIEW, ERROR},
    NEEDS_REVIEW: {TAGGING},
    TAGGING: {NEEDS_REVIEW},
    ERROR: {QUEUED, RESOLVING},
}

# Where a track goes if the app stopped while it was mid-flight. Tagging goes
# back to review (the downloaded file and candidates are still there);
# everything else restarts its download cleanly.
RESET_ON_STARTUP = {
    FETCHING_INFO: QUEUED,
    DOWNLOADING: QUEUED,
    ANALYZING: QUEUED,
    TAGGING: NEEDS_REVIEW,
}


def can_transition(current, new):
    return new in TRANSITIONS.get(current, ())


def is_deletable(status):
    return status not in NOT_DELETABLE


def for_client():
    """The groups the web UI needs, so it never keeps its own copy."""
    return {"in_progress": list(IN_PROGRESS), "not_deletable": list(NOT_DELETABLE)}
