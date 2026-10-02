"""Regras de negócio do Central, separadas das rotas HTTP.

Por que esta pasta existe: as rotas (`backend/routers/`) cuidam de HTTP —
ler o corpo da requisição, devolver o código certo. Quem sabe o que é uma
cerca válida, como calcular área ou como montar os bytes canônicos são os
módulos daqui. Isso permite testar as regras sem subir servidor nenhum, e
é o que faz os testes da fase F2 rodarem em milissegundos.
"""
