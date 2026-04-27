"""Precise math + physics via SymPy.

A single `calculate(code)` tool evaluates Python/SymPy expressions in a
sandboxed namespace pre-loaded with all of SymPy, physics units, and
common constants. Returns exact symbolic results when possible, decimal
approximations when asked.
"""
import asyncio
import builtins
import logging

import sympy
from sympy.physics import units as _u
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("friday-agent")

# All public SymPy names — gives the LLM solve, integrate, diff, simplify,
# Matrix, Symbol, Eq, sin/cos/exp/log, pi/E/I/oo, Rational, etc., for free.
_SYMPY_NS = {k: v for k, v in vars(sympy).items() if not k.startswith("_")}

# Pre-defined symbols so the LLM doesn't have to declare basics.
_x, _y, _z, _t, _n, _k = sympy.symbols("x y z t n k")

# Physics units + constants. Names chosen for natural readability.
_PHYSICS_NS = {
    "units": _u,
    "convert_to": _u.convert_to,
    # Length
    "meter": _u.meter, "kilometer": _u.kilometer, "centimeter": _u.centimeter,
    "millimeter": _u.millimeter, "mile": _u.mile, "foot": _u.foot, "inch": _u.inch,
    # Mass
    "kilogram": _u.kilogram, "gram": _u.gram, "pound": _u.pound,
    # Time
    "second": _u.second, "minute": _u.minute, "hour": _u.hour,
    "day": _u.day, "year": _u.year,
    # Mechanics
    "newton": _u.newton, "joule": _u.joule, "watt": _u.watt, "pascal": _u.pascal,
    # EM
    "volt": _u.volt, "ampere": _u.ampere, "ohm": _u.ohm, "coulomb": _u.coulomb,
    "tesla": _u.tesla, "henry": _u.henry, "farad": _u.farad,
    # Temperature
    "kelvin": _u.kelvin,
    # Physics constants
    "c": _u.speed_of_light,
    "G": _u.gravitational_constant,
    "h": _u.planck,
    "hbar": _u.hbar,
    "e_charge": _u.elementary_charge,
    "k_B": _u.boltzmann,
    "N_A": _u.avogadro_number,
    "R_gas": _u.molar_gas_constant,
    "m_e": _u.electron_rest_mass,
    "eps_0": _u.vacuum_permittivity,
    "mu_0": _u.vacuum_permeability,
}

_NAMESPACE = {
    **_SYMPY_NS,
    **_PHYSICS_NS,
    "x": _x, "y": _y, "z": _z, "t": _t, "n": _n, "k": _k,
}

# Restrict builtins — block import, open, eval, exec, compile, etc.
_SAFE_BUILTINS = {
    name: getattr(builtins, name)
    for name in (
        "abs", "all", "any", "bool", "complex", "divmod", "enumerate",
        "filter", "float", "int", "isinstance", "len", "list", "map",
        "max", "min", "pow", "range", "reversed", "round", "set",
        "sorted", "str", "sum", "tuple", "zip", "True", "False", "None",
    )
}

_TIMEOUT_SECONDS = 8.0
_MAX_OUTPUT_CHARS = 2000


def _evaluate(code: str) -> str:
    code = code.strip()
    if not code:
        return "Empty expression."
    try:
        result = eval(code, {"__builtins__": _SAFE_BUILTINS}, _NAMESPACE)
    except SyntaxError:
        # Fall back to exec for multi-statement code; pull the last expression
        # from a `result` or `_` variable.
        try:
            local_ns = dict(_NAMESPACE)
            exec(code, {"__builtins__": _SAFE_BUILTINS}, local_ns)
            result = local_ns.get("result", local_ns.get("_", "(no return value — assign to `result`)"))
        except Exception as e:
            return f"Math error: {type(e).__name__}: {e}"
    except Exception as e:
        return f"Math error: {type(e).__name__}: {e}"

    text = str(result)
    if len(text) > _MAX_OUTPUT_CHARS:
        text = text[: _MAX_OUTPUT_CHARS - 20] + " … [truncated]"
    return text


def register(mcp: FastMCP):

    @mcp.tool(name="calculate")
    async def calculate(code: str) -> str:
        """Evaluate a SymPy/Python math expression precisely.

        Use whenever precision matters or the math is non-trivial: arithmetic
        with fractions, algebra, calculus, equation solving, unit-aware
        physics, matrix ops. Pair with `read_screen` when the problem is
        visible on the user's display.

        The execution namespace pre-loads all of SymPy plus physics units
        and constants:
          - units: meter, kilometer, second, hour, kilogram, joule, newton,
            volt, ampere, ohm, kelvin, pascal, watt, etc.
          - constants: c, G, h, hbar, e_charge, k_B, N_A, R_gas, m_e,
            eps_0, mu_0
          - symbols: x, y, z, t, n, k pre-defined; declare more via
            `symbols('a b c')`
          - convert_to(value, target_units) for unit conversion

        Examples:
          calculate("Rational(7,3) + Rational(1,9)")        → "22/9"  (exact)
          calculate("7/3 + 1/9")                            → "2.444…"  (float)
          calculate("solve(x**2 + 5*x - 6, x)")             → "[-6, 1]"
          calculate("integrate(sin(x)**2, (x, 0, pi))")     → "pi/2"
          calculate("diff(x**3 * sin(x), x)")               → "x**3*cos(x) + 3*x**2*sin(x)"
          calculate("convert_to(60*mile/hour, meter/second).n(4)") → "26.82 m/s"
          calculate("convert_to(G * 5.972e24*kilogram / (6.371e6*meter)**2, meter/second**2).n(4)")
              → "9.819 m/s**2"  (gravity at Earth's surface)

        Quirks to know:
          - Python `/` floats integers first; use `Rational(a,b)` for exact.
          - Unit-bearing results need `convert_to(expr, target_unit)` before
            calling `.n()` or `float()` to extract a number.
          - For a plain decimal of a unitless expression: wrap in `N(..., 6)`
            or `float(...)`.
        """
        try:
            return await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, _evaluate, code),
                timeout=_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            return f"Computation timed out after {_TIMEOUT_SECONDS:.0f}s. Try a simpler form."
