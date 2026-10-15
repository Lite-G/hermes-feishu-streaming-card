"""Verified Hermes v0.22.1+ split turn-mixin contracts (HFC #428).

Hermes 297552a9d6 moved the completion/agent:end seam of
``GatewayTurnMixin`` into ``GatewayTurnPersistenceMixin``
(``gateway/run_turn_persistence.py``), moved ``TurnRunner._status_callback_sync``
into ``GatewayTurnProgressMixin`` (``gateway/run_turn_progress.py``) and turned
``TurnRunner._approval_notify_sync`` into a wrapper around
``_render_approval_sync``. These predicates accept only those exact shapes.
They are fallbacks: callers consult them only when the established
single-class anchors are absent, so earlier layouts keep their exact output.
Pure AST checks; never import Hermes, execute source or touch files.
"""
from __future__ import annotations

import ast
from typing import Any, Mapping

from .ast_contracts import _same_expression

PERSISTENCE_TARGET = "gateway/run_turn_persistence.py"
PROGRESS_TARGET = "gateway/run_turn_progress.py"
SPLIT_TURN_TARGETS = (PERSISTENCE_TARGET, PROGRESS_TARGET)
HANDLER_TARGET = "gateway/run_turn.py"
RUNNER_TARGET = "gateway/run_turn_runner.py"

PERSISTENCE_MIXIN = "GatewayTurnPersistenceMixin"
PROGRESS_MIXIN = "GatewayTurnProgressMixin"
_HANDLER_NAME = "_handle_message_with_agent"

_DELIVERY_PARAMETERS = (
    "self", "event", "source", "session_entry", "session_key", "run_generation",
    "agent_result", "agent_messages", "response", "_footer_line", "_intentional_silence",
)
_POST_PARAMETERS = ("self", "hook_ctx", "agent_result", "response")
_EXPECTED_RETURN = (
    "return await self._hmwa_deliver_turn_response(event, source, session_entry, "
    "session_key, run_generation, agent_result, agent_messages, response, "
    "_footer_line, _intentional_silence)"
)
_EXPECTED_POST = "await self._hmwa_post_turn_hooks(hook_ctx, agent_result, response)"
_SPLIT_METHODS = ("_hmwa_deliver_turn_response", "_hmwa_post_turn_hooks")


def _parse(content: str | None) -> ast.Module | None:
    if not content:
        return None
    try:
        return ast.parse(content)
    except SyntaxError:
        return None


def _top_classes(tree: ast.Module, name: str) -> list[ast.ClassDef]:
    return [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name]


def _direct_methods(class_node: ast.ClassDef, name: str) -> list[Any]:
    return [n for n in class_node.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]


def _exact_parameters(node: Any, parameters: tuple[str, ...]) -> bool:
    args = node.args
    return (tuple(a.arg for a in (*args.posonlyargs, *args.args)) == parameters
            and not args.kwonlyargs and args.vararg is None and args.kwarg is None
            and not args.defaults and not args.kw_defaults)


def _imports_from(tree: ast.Module, module: str, name: str) -> bool:
    """Exactly one unaliased top-level ``from <module> import <name>``."""
    hits = [alias for node in tree.body if isinstance(node, ast.ImportFrom)
            and node.module == module and node.level == 0
            for alias in node.names if alias.name == name]
    return len(hits) == 1 and hits[0].asname is None


def _binds_name_elsewhere(tree: ast.Module, name: str) -> bool:
    """Any definition/assignment of ``name`` other than the verified import."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return True
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, (ast.Store, ast.Del)):
            return True
    return False


def _inherits_verified_mixin(tree: ast.Module, class_node: ast.ClassDef, module: str, mixin: str) -> bool:
    bases = [b for b in class_node.bases if isinstance(b, ast.Name) and b.id == mixin]
    return (len(bases) == 1 and not class_node.keywords
            and _imports_from(tree, module, mixin) and not _binds_name_elsewhere(tree, mixin))


def _awaited_emits(node: Any, event: str) -> list[ast.Await]:
    return [n for n in ast.walk(node) if isinstance(n, ast.Await)
            and isinstance(n.value, ast.Call)
            and _same_expression(n.value.func, "self.hooks.emit")
            and n.value.args and _same_expression(n.value.args[0], repr(event).replace("'", '"'))]


# -- completion / agent:end -------------------------------------------------

def find_split_completion_delivery(tree: ast.Module) -> Any:
    """``_hmwa_deliver_turn_response`` of the exact persistence mixin, or None."""
    if any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == _HANDLER_NAME
           for n in ast.walk(tree)):
        return None
    classes = _top_classes(tree, PERSISTENCE_MIXIN)
    if len(classes) != 1:
        return None
    mixin = classes[0]
    found = []
    for name, parameters in zip(_SPLIT_METHODS, (_DELIVERY_PARAMETERS, _POST_PARAMETERS)):
        candidates = _direct_methods(mixin, name)
        if (len(candidates) != 1 or not isinstance(candidates[0], ast.AsyncFunctionDef)
                or not _exact_parameters(candidates[0], parameters)):
            return None
        found.append(candidates[0])
    # The methods must exist exactly once in the whole module (no shadowing).
    for name in _SPLIT_METHODS:
        if sum(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
               for n in ast.walk(tree)) != 1:
            return None
    delivery, post = found
    if len(_awaited_emits(post, "agent:end")) != 1:
        return None
    return delivery


def split_completion_caller_verified(handler_content: str | None) -> bool:
    """The handler file calls the split seams in order and inherits the exact mixin."""
    tree = _parse(handler_content)
    if tree is None:
        return False
    owners = [n for n in tree.body if isinstance(n, ast.ClassDef)
              and _direct_methods(n, _HANDLER_NAME)]
    if len(owners) != 1:
        return False
    owner = owners[0]
    handlers = _direct_methods(owner, _HANDLER_NAME)
    if len(handlers) != 1 or not isinstance(handlers[0], ast.AsyncFunctionDef):
        return False
    if any(_direct_methods(owner, name) for name in _SPLIT_METHODS):
        return False  # the established single-class contract owns this layout
    if not _inherits_verified_mixin(tree, owner, "gateway.run_turn_persistence", PERSISTENCE_MIXIN):
        return False
    handler = handlers[0]
    expected_return = ast.dump(ast.parse(_EXPECTED_RETURN).body[0])
    expected_post = ast.dump(ast.parse(_EXPECTED_POST).body[0])
    returns = [n for n in ast.walk(handler) if isinstance(n, ast.Return) and ast.dump(n) == expected_return]
    posts = [n for n in ast.walk(handler) if isinstance(n, ast.Expr) and ast.dump(n) == expected_post]
    if len(returns) != 1 or len(posts) != 1 or posts[0].lineno >= returns[0].lineno:
        return False
    # No other call sites may bypass or duplicate the verified seam.
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr in _SPLIT_METHODS]
    return len(calls) == 2


def split_completion_verified(sources: Mapping[str, str]) -> bool:
    tree = _parse(sources.get(PERSISTENCE_TARGET))
    return (tree is not None and find_split_completion_delivery(tree) is not None
            and split_completion_caller_verified(sources.get(HANDLER_TARGET)))


# -- status ----------------------------------------------------------------

def find_split_status_callback(tree: ast.Module) -> Any:
    """``GatewayTurnProgressMixin._status_callback_sync`` (exact signature), or None."""
    if any(isinstance(n, ast.ClassDef) and n.name == "TurnRunner" for n in tree.body):
        return None
    classes = _top_classes(tree, PROGRESS_MIXIN)
    if len(classes) != 1:
        return None
    methods = _direct_methods(classes[0], "_status_callback_sync")
    if (len(methods) != 1 or not isinstance(methods[0], ast.FunctionDef)
            or not _exact_parameters(methods[0], ("self", "event_type", "message"))):
        return None
    if sum(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_status_callback_sync"
           for n in ast.walk(tree)) != 1:
        return None
    return methods[0]


def split_status_runner_verified(runner_content: str | None) -> bool:
    tree = _parse(runner_content)
    if tree is None:
        return False
    runners = _top_classes(tree, "TurnRunner")
    if len(runners) != 1:
        return False
    runner = runners[0]
    if any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_status_callback_sync"
           for n in ast.walk(runner)):
        return False
    return _inherits_verified_mixin(tree, runner, "gateway.run_turn_progress", PROGRESS_MIXIN)


def split_status_verified(sources: Mapping[str, str]) -> bool:
    tree = _parse(sources.get(PROGRESS_TARGET))
    return (tree is not None and find_split_status_callback(tree) is not None
            and split_status_runner_verified(sources.get(RUNNER_TARGET)))


# -- approval --------------------------------------------------------------

def find_delegated_approval_renderer(turn_runner: ast.ClassDef) -> Any:
    """``_render_approval_sync`` when ``_approval_notify_sync`` only delegates to it.

    Accepted shape (Hermes 297552a9d6): the wrapper has the exact
    ``(self, approval_data)`` signature, contains exactly one statement
    ``self._render_approval_sync(approval_data)`` in its direct body, and the
    renderer is a single direct ``TurnRunner`` method with the same signature
    that nothing else in the class calls.
    """
    wrappers = _direct_methods(turn_runner, "_approval_notify_sync")
    renderers = _direct_methods(turn_runner, "_render_approval_sync")
    if len(wrappers) != 1 or len(renderers) != 1:
        return None
    wrapper, renderer = wrappers[0], renderers[0]
    if not all(isinstance(n, ast.FunctionDef) and _exact_parameters(n, ("self", "approval_data"))
               for n in (wrapper, renderer)):
        return None
    expected = ast.dump(ast.parse("self._render_approval_sync(approval_data)").body[0])
    direct = [s for s in wrapper.body if ast.dump(s) == expected]
    if len(direct) != 1:
        return None
    references = [n for n in ast.walk(turn_runner)
                  if isinstance(n, ast.Attribute) and n.attr == "_render_approval_sync"]
    if len(references) != 1:
        return None
    return renderer
