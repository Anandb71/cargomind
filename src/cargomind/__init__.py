"""CargoMind - self-healing multi-agent Logistics Handler (Google ADK + Gemini + MCP).

`adk web src` discovers this package; `app`/`root_agent` live in cargomind.agent.
"""
__version__ = "0.1.0"


def __getattr__(name):  # lazy so `import cargomind.config` stays cheap
    if name in ("app", "root_agent"):
        from cargomind import agent

        return getattr(agent, name)
    raise AttributeError(name)
