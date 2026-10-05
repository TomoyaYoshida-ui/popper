# External DomainPack example

This is a separate Python distribution. It demonstrates how to add a domain
metric without editing Popper's built-in registry. It reuses Popper's existing
numerical convergence scoring and does not introduce a new scientific method.

```powershell
python -m pip wheel . --no-deps -w .\wheelhouse
$wheel = (Get-ChildItem .\wheelhouse\popper_example_external_order-*.whl | Select-Object -First 1).FullName
python -m pip install $wheel
$env:POPPER_DOMAIN_PACKS = "external-order"
```

Run these commands from this directory after Popper itself is installed. Copy
`examples/ode-convergence` as a project, change `metric.name` in its
`experiment.json` to `external_convergence_order`, then initialize and run it
with `popper experiment init`, `search --trusted-local`, `freeze`, `confirm
--trusted-local`, and `replay`. Do not initialize the original example before
changing its metric: initialized inputs are immutable.

The environment variable must also be set when replaying the project on a new
machine. Entry-point loading runs code with your local Python account's
permissions; only select audited packages you trust. Pin the exact external
distribution version when sharing evidence. This is an experimental extension
surface, not a signed or sandboxed plugin system. An editable install can be
useful during development, but its path-dependent installation files may make
the distribution digest non-portable; use the same wheel for independent replay.
