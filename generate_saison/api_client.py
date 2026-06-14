"""
Client HTTP minimal pour l'API du backend (Spring Boot).

Un ApiClient = une session authentifiée (un token). Le bootstrap en crée
plusieurs : le président, chaque "worker" (rôles d'écriture spécialisés) et un
client par joueur (pour la saisie wellness/RPE côté JOUEUR).
"""

from __future__ import annotations

import time
from typing import Any

import requests


class ApiError(RuntimeError):
    def __init__(self, methode: str, url: str, statut: int, corps: str):
        super().__init__(f"{methode} {url} → HTTP {statut} : {corps[:400]}")
        self.statut = statut
        self.corps = corps


class ApiClient:
    def __init__(self, base_url: str, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.token: str | None = None
        self.auth: dict[str, Any] = {}
        self._ctx_club: str | None = None
        self._ctx_equipes: list[str] = []

    # ── Authentification ──
    def login(self, email: str, mot_de_passe: str) -> dict[str, Any]:
        url = f"{self.base_url}/api/auth/login"
        r = self.session.post(url, json={"email": email, "motDePasse": mot_de_passe},
                              timeout=self.timeout)
        if r.status_code != 200:
            raise ApiError("POST", url, r.status_code, r.text)
        self.auth = r.json()
        self.token = self.auth.get("token")
        return self.auth

    # ── Contexte de navigation (en-têtes multi-tenant) ──
    def set_contexte(self, club_id: str | None = None, equipe_ids: list[str] | None = None) -> "ApiClient":
        self._ctx_club = club_id
        self._ctx_equipes = equipe_ids or []
        return self

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if self._ctx_club:
            h["X-Contexte-Club"] = self._ctx_club
        if self._ctx_equipes:
            h["X-Contexte-Equipes"] = ",".join(self._ctx_equipes)
        return h

    # ── Verbes HTTP ──
    def _requete(self, methode: str, path: str, *, json: Any = None,
                 params: dict | None = None, ok=(200, 201, 204)) -> Any:
        url = f"{self.base_url}{path}"
        derniere_exc: Exception | None = None
        for tentative in range(3):
            try:
                r = self.session.request(methode, url, json=json, params=params,
                                         headers=self._headers(), timeout=self.timeout)
            except requests.RequestException as e:
                derniere_exc = e
                time.sleep(1.5 * (tentative + 1))
                continue
            if r.status_code in ok:
                if r.status_code == 204 or not r.content:
                    return None
                try:
                    return r.json()
                except ValueError:
                    return r.text
            raise ApiError(methode, url, r.status_code, r.text)
        raise ApiError(methode, url, -1, f"échec réseau : {derniere_exc}")

    def get(self, path: str, params: dict | None = None) -> Any:
        return self._requete("GET", path, params=params)

    def post(self, path: str, json: Any = None) -> Any:
        return self._requete("POST", path, json=json)

    def put(self, path: str, json: Any = None) -> Any:
        return self._requete("PUT", path, json=json)

    def patch(self, path: str, json: Any = None) -> Any:
        return self._requete("PATCH", path, json=json)

    def delete(self, path: str) -> Any:
        return self._requete("DELETE", path)
