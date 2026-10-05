"""Fixed RK4 control solver using the same host RHS interface as model code.

This is the preregistered control, not a proposed winning method. No reference
solution, oscillator parameters or candidate-reported query count is needed.
"""
import math


def solve(rhs, y0, times, budget, config):
    steps_per_interval = budget // (4 * (len(times) - 1))
    if steps_per_interval < 1:
        raise ValueError("budget too small for sample schedule")
    state = list(y0)
    states = [state.copy()]
    for start, stop in zip(times, times[1:]):
        h = (stop - start) / steps_per_interval
        for step in range(steps_per_interval):
            t = start + step * h
            a = rhs(t, state)
            b = rhs(t + h / 2, [x + h * k / 2 for x, k in zip(state, a)])
            c = rhs(t + h / 2, [x + h * k / 2 for x, k in zip(state, b)])
            d = rhs(t + h, [x + h * k for x, k in zip(state, c)])
            state = [x + h * (ka + 2 * kb + 2 * kc + kd) / 6
                     for x, ka, kb, kc, kd in zip(state, a, b, c, d)]
            if any(not math.isfinite(x) or abs(x) > 1e100 for x in state):
                return None
        states.append(state.copy())
    return states
