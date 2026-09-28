"""The Builds division: Daedalus builds small developer tools overnight, Claude reviews
every build, and each approved one is staged on the product shelf for the owner's approval.

- ``window.py``   the overnight GPU window (default 01:00-07:00) and the per-job budget
- ``backlog.py``  the product ideas (seeded; the owner edits them by replying on Discord)
- ``sandbox.py``  each product's own fresh git repo under the sandbox workspace
- ``review.py``   our own checks (tests run by us, no network, no secrets) and Claude's review
- ``package.py``  an approved build staged in exactly the product shelf's format
- ``worker.py``   ``builds.daedalus``, the worker that runs it all
"""
