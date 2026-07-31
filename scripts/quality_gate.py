#!/usr/bin/env python
"""hdh design-quality gate.

A learning-focused static check that enforces design principles, not just
style (ruff) or types (mypy). Each check maps to a principle:

  contracts        Public classes and non-trivial functions carry docstrings,
                   so every module boundary states what it promises.
  no-god-class     Classes and functions stay small with one responsibility.
  pluggability     Every CLI module complies with the register_cli interface,
                   so implementations can coexist and be discovered.
  dependency-injection
                   Modules receive their collaborators (DB sessions, API
                   clients) instead of constructing them; only composition
                   roots may build dependencies.
  immutability     No mutable default arguments; module-level constants
                   prefer immutable types (tuple over list).
  injection-safety Inputs must not reach eval/exec/shell/SQL unvalidated:
                   no eval/exec, no shell=True/os.system, no f-string or
                   concatenated SQL passed to text()/execute().
  data-abstraction Public APIs should return typed structures (dataclasses)
                   rather than bare dicts, so contracts are explicit.

The checker practices what it preaches: checks are pluggable implementations
of the QualityCheck protocol, findings are frozen (immutable) dataclasses,
and each check receives its inputs (the parsed module) via injection.

Waivers: a justified exception is marked inline with
``# quality: allow(<check-name>)`` on the offending line — visible in review,
never global. Errors fail the build; warnings are advisory.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

SRC = Path("src/hdh")

# Composition roots: the only files allowed to construct dependencies
# (engines, sessions, API clients) rather than receive them.
COMPOSITION_ROOTS = {
    "src/hdh/cli.py",  # wires session into every subcommand
    "src/hdh/core/models.py",  # defines the engine/session factories
    "src/hdh/modules/fhir_api/server.py",  # per-request session lifecycle
    "src/hdh/modules/agent/pipeline/gateway.py",  # pipeline composition root
}

DEPENDENCY_FACTORIES = {"get_engine", "get_session", "create_engine", "Anthropic", "AsyncAnthropic"}

MAX_CLASS_METHODS = 12
MAX_CLASS_LINES = 400
MAX_FUNCTION_LINES = 150
WARN_FUNCTION_LINES = 80
MAX_FUNCTION_PARAMS = 6
DOCSTRING_MIN_FUNC_BODY = 10  # functions longer than this need a docstring
DOCSTRING_MIN_CLASS_BODY = 8  # classes longer than this need a docstring


@dataclass(frozen=True, order=True)
class Finding:
    """One violation: immutable, self-describing, sortable."""

    check: str
    principle: str
    severity: str  # "error" | "warn"
    path: str
    line: int
    message: str


@dataclass(frozen=True)
class Module:
    """A parsed source file — the unit of analysis injected into checks."""

    path: str
    tree: ast.Module
    lines: tuple[str, ...]

    def waived(self, check: str, line: int) -> bool:
        """True if the line carries an inline `# quality: allow(<check>)` waiver."""
        if 0 < line <= len(self.lines):
            return f"quality: allow({check})" in self.lines[line - 1]
        return False


class QualityCheck(Protocol):
    """The interface every check implements — new checks plug in beside these."""

    name: str
    principle: str

    def run(self, module: Module) -> list[Finding]:
        """Analyze one module and return findings."""
        ...


def _finding(check: QualityCheck, severity: str, module: Module, line: int, message: str) -> Finding:
    return Finding(check.name, check.principle, severity, module.path, line, message)


def _body_lines(node: ast.AST) -> int:
    return (getattr(node, "end_lineno", 0) or 0) - node.lineno


def _is_public(name: str) -> bool:
    return not name.startswith("_")


class ContractCheck:
    """Docstrings on public classes and non-trivial public functions."""

    name = "contracts"
    principle = "Clear contracts between classes and modules"

    def run(self, module: Module) -> list[Finding]:
        """Flag missing module/class/function docstrings on public API.

        Only module-level definitions and class methods form the public
        contract; nested helper functions are implementation detail.
        """
        findings = []
        if not ast.get_docstring(module.tree):
            findings.append(_finding(self, "error", module, 1, "module has no docstring"))
        api: list[ast.AST] = list(module.tree.body)
        api += [n for cls in module.tree.body if isinstance(cls, ast.ClassDef) for n in cls.body]
        for node in api:
            if isinstance(node, ast.ClassDef) and _is_public(node.name):
                if _body_lines(node) > DOCSTRING_MIN_CLASS_BODY and not ast.get_docstring(node):
                    findings.append(
                        _finding(
                            self,
                            "error",
                            module,
                            node.lineno,
                            f"public class {node.name} has no docstring — state its contract",
                        )
                    )
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and _is_public(node.name):
                if _body_lines(node) > DOCSTRING_MIN_FUNC_BODY and not ast.get_docstring(node):
                    findings.append(
                        _finding(
                            self,
                            "error",
                            module,
                            node.lineno,
                            f"public function {node.name}() has no docstring — state its contract",
                        )
                    )
        return [f for f in findings if not module.waived(self.name, f.line)]


class GodClassCheck:
    """Size limits that keep responsibilities singular."""

    name = "no-god-class"
    principle = "Clear responsibilities — no god classes"

    def run(self, module: Module) -> list[Finding]:
        """Flag oversized classes/functions and over-parameterized signatures."""
        findings = []
        for node in ast.walk(module.tree):
            if isinstance(node, ast.ClassDef):
                methods = [
                    n
                    for n in node.body
                    if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and _is_public(n.name)
                ]
                if len(methods) > MAX_CLASS_METHODS:
                    findings.append(
                        _finding(
                            self,
                            "error",
                            module,
                            node.lineno,
                            f"class {node.name} has {len(methods)} public methods "
                            f"(max {MAX_CLASS_METHODS}) — split responsibilities",
                        )
                    )
                if _body_lines(node) > MAX_CLASS_LINES:
                    findings.append(
                        _finding(
                            self,
                            "error",
                            module,
                            node.lineno,
                            f"class {node.name} is {_body_lines(node)} lines "
                            f"(max {MAX_CLASS_LINES}) — split responsibilities",
                        )
                    )
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                size = _body_lines(node)
                if size > MAX_FUNCTION_LINES:
                    findings.append(
                        _finding(
                            self,
                            "error",
                            module,
                            node.lineno,
                            f"function {node.name}() is {size} lines (max {MAX_FUNCTION_LINES})",
                        )
                    )
                elif size > WARN_FUNCTION_LINES:
                    findings.append(
                        _finding(
                            self,
                            "warn",
                            module,
                            node.lineno,
                            f"function {node.name}() is {size} lines — consider extracting helpers",
                        )
                    )
                n_params = len(node.args.args) + len(node.args.kwonlyargs)
                if node.args.args and node.args.args[0].arg in ("self", "cls"):
                    n_params -= 1
                if n_params > MAX_FUNCTION_PARAMS:
                    findings.append(
                        _finding(
                            self,
                            "error",
                            module,
                            node.lineno,
                            f"function {node.name}() takes {n_params} parameters "
                            f"(max {MAX_FUNCTION_PARAMS}) — group them into a structure",
                        )
                    )
        return [f for f in findings if not module.waived(self.name, f.line)]


class PluggabilityCheck:
    """CLI modules must comply with the register_cli plug-in interface."""

    name = "pluggability"
    principle = "Pluggable code behind a shared interface"

    def run(self, module: Module) -> list[Finding]:
        """Every modules/*/cli.py must define register_cli(subparsers)."""
        if not (module.path.startswith("src/hdh/modules/") and module.path.endswith("cli.py")):
            return []
        for node in module.tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "register_cli":
                return []
        return [
            _finding(
                self,
                "error",
                module,
                1,
                "CLI module does not define register_cli(subparsers) — it cannot be discovered by hdh.cli",
            )
        ]


class DependencyInjectionCheck:
    """Dependencies are received, not constructed (outside composition roots)."""

    name = "dependency-injection"
    principle = "Dependency injection"

    def run(self, module: Module) -> list[Finding]:
        """Flag construction of engines/sessions/clients outside composition roots."""
        if module.path in COMPOSITION_ROOTS:
            return []
        findings = []
        for node in ast.walk(module.tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name in DEPENDENCY_FACTORIES and not module.waived(self.name, node.lineno):
                findings.append(
                    _finding(
                        self,
                        "error",
                        module,
                        node.lineno,
                        f"{name}() constructed here — inject it from the composition "
                        f"root instead (or waive with a justification comment)",
                    )
                )
        return findings


class ImmutabilityCheck:
    """Prefer immutable data: no mutable defaults; tuples for constants."""

    name = "immutability"
    principle = "Immutable data structures wherever possible"

    _MUTABLE_LITERALS = (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)

    def run(self, module: Module) -> list[Finding]:
        """Flag mutable default arguments (error) and list-typed constants (warn)."""
        findings = []
        for node in ast.walk(module.tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                for default in list(node.args.defaults) + list(node.args.kw_defaults):
                    if isinstance(default, self._MUTABLE_LITERALS):
                        findings.append(
                            _finding(
                                self,
                                "error",
                                module,
                                node.lineno,
                                f"function {node.name}() has a mutable default argument — "
                                f"shared state across calls; use None or a frozen value",
                            )
                        )
        for node in module.tree.body:  # module-level constants only
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.List):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.isupper():
                        findings.append(
                            _finding(
                                self,
                                "warn",
                                module,
                                node.lineno,
                                f"constant {target.id} is a mutable list — prefer a tuple",
                            )
                        )
        return [f for f in findings if not module.waived(self.name, f.line)]


class InjectionSafetyCheck:
    """Inputs must never reach eval/exec/shell/SQL without validation."""

    name = "injection-safety"
    principle = "Inputs validated against injection"

    _SQL_SINKS = {"text", "execute", "executescript"}

    def run(self, module: Module) -> list[Finding]:
        """Flag eval/exec, shell execution, and dynamically-built SQL strings."""
        findings = []
        for node in ast.walk(module.tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")

            if name in ("eval", "exec") and isinstance(func, ast.Name):
                findings.append(_finding(self, "error", module, node.lineno, f"{name}() is forbidden"))
            elif name == "system" and isinstance(func, ast.Attribute):
                findings.append(
                    _finding(
                        self,
                        "error",
                        module,
                        node.lineno,
                        "os.system() is forbidden — use subprocess with a list argv",
                    )
                )
            elif name in ("run", "Popen", "call", "check_output", "check_call"):
                for kw in node.keywords:
                    if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value:
                        findings.append(
                            _finding(
                                self,
                                "error",
                                module,
                                node.lineno,
                                "subprocess with shell=True — injection risk; pass a list argv",
                            )
                        )
            elif name in self._SQL_SINKS:
                for arg in node.args:
                    if isinstance(arg, ast.JoinedStr | ast.BinOp) or (
                        isinstance(arg, ast.Call)
                        and isinstance(arg.func, ast.Attribute)
                        and arg.func.attr == "format"
                    ):
                        findings.append(
                            _finding(
                                self,
                                "error",
                                module,
                                node.lineno,
                                f"dynamically built SQL passed to {name}() — "
                                f"use bound parameters, never string interpolation",
                            )
                        )
        return [f for f in findings if not module.waived(self.name, f.line)]


class DataAbstractionCheck:
    """Public APIs should expose typed structures, not bare dicts."""

    name = "data-abstraction"
    principle = "Abstracted data structures for reusability"

    def run(self, module: Module) -> list[Finding]:
        """Warn when a public function's return annotation is dict / list[dict]."""
        findings = []
        for node in ast.walk(module.tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if not _is_public(node.name) or node.returns is None:
                continue
            annotation = ast.unparse(node.returns)
            if annotation in ("dict", "list[dict]", "dict | None"):
                findings.append(
                    _finding(
                        self,
                        "warn",
                        module,
                        node.lineno,
                        f"{node.name}() returns bare `{annotation}` — a dataclass or "
                        f"TypedDict would make the contract explicit",
                    )
                )
        return [f for f in findings if not module.waived(self.name, f.line)]


CHECKS: tuple[QualityCheck, ...] = (
    ContractCheck(),
    GodClassCheck(),
    PluggabilityCheck(),
    DependencyInjectionCheck(),
    ImmutabilityCheck(),
    InjectionSafetyCheck(),
    DataAbstractionCheck(),
)


def load_modules(root: Path) -> list[Module]:
    """Parse every Python file under root into an immutable Module."""
    modules = []
    for path in sorted(root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        modules.append(
            Module(
                path=path.as_posix(),
                tree=ast.parse(source, filename=str(path)),
                lines=tuple(source.splitlines()),
            )
        )
    return modules


def main() -> int:
    """Run every check over src/hdh; errors fail the build, warnings advise."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    modules = load_modules(SRC)
    findings = [f for module in modules for check in CHECKS for f in check.run(module)]
    errors = sorted(f for f in findings if f.severity == "error")
    warns = sorted(f for f in findings if f.severity == "warn")

    print("── design-quality gate ────────────────────────────────────")
    print(
        f"   {len(modules)} modules · {len(CHECKS)} checks · {len(errors)} errors · {len(warns)} warnings\n"
    )

    def show(group: list[Finding], label: str) -> None:
        by_principle: dict[str, list[Finding]] = {}
        for f in group:
            by_principle.setdefault(f.principle, []).append(f)
        for principle, items in by_principle.items():
            print(f" {label} {principle}")
            for f in items:
                print(f"     {f.path}:{f.line}  {f.message}")
            print()

    show(errors, "❌")
    show(warns, "⚠️ ")

    if errors:
        print("❌ design-quality gate failed — fix the errors above or add an")
        print("   inline `# quality: allow(<check>)` with a justification.")
        return 1
    print(f"✅ design-quality gate passed ({len(warns)} advisory warnings)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
