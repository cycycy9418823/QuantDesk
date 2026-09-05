# QuantDesk Paper

Research-only paper/account-book package index. It has no broker connection and
does not place real orders.

`matching.py` is the shared fill-policy *declaration* table, not a finished
matching engine. Each paper ledger header declares `next_open` or
`daily_approx`; the two books must not impersonate each other. Old fill paths
and their evidence limits remain until a later §4 change.
