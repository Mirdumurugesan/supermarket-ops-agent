"""Guardrail violations raised by the service layer.

Tools catch these and return a clear refusal to the model, which then explains
the problem to the owner in plain language. The rule is enforced where the
data changes — the model can only relay it, never bypass it.
"""


class KiranaError(Exception):
    """Base class for business-rule refusals."""


class OversellError(KiranaError):
    pass


class GuardrailError(KiranaError):
    pass


class NotFoundError(KiranaError):
    pass
