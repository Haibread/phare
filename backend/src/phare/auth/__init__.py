"""Authentication providers: the seam for "Sign in with <source>" identity flows.

The first implementation is Plex (PIN auth + server-membership gate). The interface is shaped so a
new provider (Trakt, generic OIDC, …) adds an implementation + a ``provider`` value, with no change
to the user model, tokens, or isolation logic. See ``docs/auth.md``.
"""
