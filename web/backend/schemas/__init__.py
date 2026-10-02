"""Modelos Pydantic: a forma dos dados que entram e saem da API.

O que o Pydantic faz aqui, e o que ele NÃO faz
----------------------------------------------
Ele confere TIPOS: que `lat` é número, que `points` é uma lista, que
`accept_warnings` é verdadeiro ou falso. E gera a documentação
automática em `/docs` a partir dessas declarações.

Ele NÃO aplica as regras VAL-01 a VAL-12, mesmo sendo capaz. A razão é o
produtor: quando o Pydantic recusa um campo, o FastAPI responde um 422
com `detail` em formato de LISTA, bem diferente do corpo definido na
seção 6 da especificação, e com mensagem em inglês do tipo
"Input should be greater than or equal to -90".

Mantendo os modelos permissivos quanto a valores, toda recusa de cerca
passa por `services/validation.py` e chega ao navegador no formato único
do contrato, com mensagem em português e os índices dos pontos a
destacar no mapa.
"""
