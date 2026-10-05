"""Third-party-style DomainPack example, separate from Popper's source tree.

This example reuses the built-in convergence measurement to demonstrate the
packaging/registration contract, not to claim a new scientific method.
"""
from popper.domains.convergence_order import ConvergenceOrderV1
from popper.domains.protocol import MetricSpec


class ExternalOrderPack(ConvergenceOrderV1):
    pack_id = "example-external-order"
    evaluator_id = "example-external-order-v1"
    _metrics = (MetricSpec(name="external_convergence_order", direction="max",
                           unit="order", description="Example external convergence metric",
                           value_domain=(0, None)),)
    _entry = {
        "id": evaluator_id,
        "metric": {"name": "external_convergence_order", "direction": "max"},
        "definition": "example external package delegates declared log-log convergence scoring",
        "dataset": "rows[id,steps,rate,t_end] with one declared refinement schedule",
    }


PACK = ExternalOrderPack()
