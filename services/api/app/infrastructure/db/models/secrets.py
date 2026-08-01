"""Encrypted broker credentials at rest.

Deliberately the plainest table in the schema: a reference and a blob. Everything that
makes a secret a secret happens in :mod:`app.infrastructure.secrets.encrypted` before the
value arrives here, and the column type says so — ``LargeBinary`` holding a Fernet token,
not ``Text`` holding something a reader might mistake for readable.

**No ``user_id``, and therefore no row-level security**, which is a deliberate exception
to the rule every other tenant table follows. Three reasons, in order of weight:

1. The row is written *before* the ``broker_connections`` row exists — the credentials are
   verified against the broker first, and only a working login gets a connection — so
   there is no tenant to scope to at insert time and no foreign key to hang one on.
2. ``reference`` is ``<broker>/<uuid7>``, minted server-side and never derived from user
   input, so it is a capability rather than a guessable address.
3. Every read goes through a ``credential_ref`` on a ``broker_connections`` row, and *that*
   table is RLS-protected. Reaching a secret requires first reading a connection the
   policy already governs.

The honest caveat is that (3) is a property of the current call sites rather than of the
database, so a future query that goes straight to this table would bypass the check. That
is the cost of (1), and it is written down here rather than left for someone to discover.
"""

from __future__ import annotations

from sqlalchemy import LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import Base, TimestampMixin


class StoredSecret(TimestampMixin, Base):
    """One encrypted credential document.

    The reference is the primary key rather than a surrogate id: there is exactly one
    secret per reference by construction, and a surrogate would allow two rows to claim
    the same reference and leave which one wins to insertion order.
    """

    __tablename__ = "stored_secrets"

    reference: Mapped[str] = mapped_column(String(200), primary_key=True)
    #: A Fernet token: AES-128-CBC ciphertext with an HMAC-SHA256 tag and a timestamp.
    #: Opaque here on purpose — nothing in the database layer can or should decrypt it.
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
