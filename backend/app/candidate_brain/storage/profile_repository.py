from app.candidate_brain.models import Candidate


class ProfileRepository:
    def __init__(self):
        self.pending: dict[str, Candidate] = {}
        self.approved: dict[str, Candidate] = {}

    def save_pending(
        self,
        candidate_id: str,
        candidate: Candidate
    ) -> None:
        self.pending[candidate_id] = candidate.model_copy(deep=True)

    def get_pending(
        self,
        candidate_id: str
    ) -> Candidate | None:
        candidate = self.pending.get(candidate_id)
        return candidate.model_copy(deep=True) if candidate else None

    def get_approved(
        self,
        candidate_id: str
    ) -> Candidate | None:
        candidate = self.approved.get(candidate_id)
        return candidate.model_copy(deep=True) if candidate else None

    def save_approved(
        self,
        candidate_id: str,
        candidate: Candidate
    ) -> None:
        self.approved[candidate_id] = candidate.model_copy(deep=True)

    def delete_pending(self, candidate_id: str) -> None:
        self.pending.pop(candidate_id, None)