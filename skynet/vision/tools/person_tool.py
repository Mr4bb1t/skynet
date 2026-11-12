"""Ferramentas de memória de pessoas expostas ao modelo."""

from __future__ import annotations

import logging
from typing import Optional

from google.genai import types

LOG = logging.getLogger("vision.tool.people")

REMEMBER_DECLARATION = types.FunctionDeclaration(
    name="remember_person",
    description=(
        "Salva ou atualiza uma pessoa na memória. Use depois de capture_image "
        "quando o usuário apresentar alguém ou pedir para lembrar/atualizar "
        "informações. Use o face_index do JSON de pessoas da última foto. "
        "Você pode informar nome, apelido e observações; os dados são mantidos "
        "localmente."
    ),
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "name": types.Schema(
                type=types.Type.STRING,
                description="Nome da pessoa (obrigatório).",
            ),
            "nickname": types.Schema(
                type=types.Type.STRING,
                description="Apelido ou como a pessoa gosta de ser chamada.",
            ),
            "notes": types.Schema(
                type=types.Type.STRING,
                description=(
                    "Observações livres: relação com o usuário, gostos, "
                    "assuntos, ressalvas, etc."
                ),
            ),
            "face_index": types.Schema(
                type=types.Type.INTEGER,
                description=(
                    "Índice do rosto no JSON da última foto (campo face_index). "
                    "Padrão: 0."
                ),
            ),
        },
        required=["name"],
    ),
)

RECALL_DECLARATION = types.FunctionDeclaration(
    name="recall_person",
    description=(
        "Consulta o que você já sabe sobre uma pessoa pelo nome, apelido ou "
        "palavra nas observações. Use quando o usuário perguntar sobre alguém."
    ),
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "query": types.Schema(
                type=types.Type.STRING,
                description="Nome, apelido ou termo para buscar.",
            ),
        },
        required=["query"],
    ),
)

LIST_DECLARATION = types.FunctionDeclaration(
    name="list_people",
    description="Lista todas as pessoas que você conhece e o que sabe sobre elas.",
    parameters=types.Schema(type=types.Type.OBJECT, properties={}),
)

FORGET_DECLARATION = types.FunctionDeclaration(
    name="forget_person",
    description=(
        "Remove uma pessoa da memória. Use somente quando o usuário pedir "
        "explicitamente para esquecer/apagar alguém."
    ),
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "name": types.Schema(
                type=types.Type.STRING,
                description="Nome ou apelido exato da pessoa a esquecer.",
            ),
        },
        required=["name"],
    ),
)


class PersonTool:
    """Wrapper das operações de memória da câmera."""

    def __init__(self, camera) -> None:
        self.camera = camera

    @property
    def available(self) -> bool:
        memory = getattr(self.camera, "memory", None)
        return bool(memory is not None and getattr(memory, "available", False))

    @property
    def reason(self) -> str:
        memory = getattr(self.camera, "memory", None)
        return str(getattr(memory, "reason", "") or "")

    def remember(
        self,
        name: str,
        nickname: str = "",
        notes: str = "",
        face_index: int = 0,
    ) -> dict:
        return self.camera.remember_person(
            name, nickname=nickname, notes=notes, face_index=face_index
        )

    def recall(self, query: str) -> dict:
        return self.camera.recall_people(query)

    def list_people(self) -> dict:
        return self.camera.list_people()

    def forget(self, name: str) -> dict:
        return self.camera.forget_person(name)
