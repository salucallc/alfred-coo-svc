"""Placeholder module for verdict data model."""


class Verdict:
    def __init__(self, verdict: str, verifier_model: str, generator_model: str, action_class: str, evidence_sha256: str, created_at: str):
        self.verdict = verdict
        self.verifier_model = verifier_model
        self.generator_model = generator_model
        self.action_class = action_class
        self.evidence_sha256 = evidence_sha256
        self.created_at = created_at
