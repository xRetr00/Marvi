"""Public room-habit API named by the learning-loops specification."""

from .room_habit import accumulate, load_state, propose, save_state, state_path


def propose_automations(histogram, **kwargs):
    return propose(histogram, **kwargs)


__all__ = ["accumulate", "load_state", "propose", "propose_automations", "save_state", "state_path"]
