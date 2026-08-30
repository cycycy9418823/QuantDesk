"""QuantDesk 8000 server package.

Modules split out of ``scripts/quant_api_server.py`` to keep the port-8000
Flask service maintainable. All public names remain re-exported from the
``quant_api_server`` namespace so the existing test attribute contract and
import sites keep working unchanged.
"""
