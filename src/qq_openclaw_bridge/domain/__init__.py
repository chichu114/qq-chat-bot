"""Pure domain models and services."""

import importlib

__all__ = [
    "ClassifierDecision",
    "ConservativeRouteResolver",
    "DeterministicRiskRouter",
    "RouteResolution",
    "RoutingContract",
    "RuleDecision",
    "parse_classifier_response",
    "redacted_preview",
]


def __getattr__(name):
    if name in __all__:
        routing = importlib.import_module(".routing", __name__)
        return getattr(routing, name)
    raise AttributeError(name)
