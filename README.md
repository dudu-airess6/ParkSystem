# ParkSystem — Sistema de Gestão de Estacionamentos

Aplicação web leve (Python + SQLite, **sem instalar nada além do Python 3.8+**).

## Como rodar
```
python app.py
```
O navegador abre em http://localhost:8000. O banco (`parksystem.db`) é criado sozinho na primeira execução.
Para recomeçar do zero, feche o programa e apague o arquivo `parksystem.db`.

## Usuários de demonstração
| Perfil            | Usuário  | Senha        |
|-------------------|----------|--------------|
| Operador de Pátio | operador | operador123  |
| ADM / Gerente     | gerente  | gerente123   |

## Casos de uso implementados
| UC   | Onde está no sistema                                              | Perfil   |
|------|-------------------------------------------------------------------|----------|
| UC01 | Tela de login (senha guardada com hash PBKDF2)                    | Todos    |
| UC02 | Registrar entrada: valida placa, vaga automática ou escolhida, ticket imprimível | Operador |
| UC03 | Registrar saída: busca por ticket/placa, pagamento, libera vaga, comprovante    | Operador |
| UC04 | Mapa de vagas (atualiza a cada 15 s; leva direto à entrada/saída) | Operador |
| UC05 | Avarias e ocorrências (vinculada ao veículo ou geral do pátio)    | Operador |
| UC06 | Mensalistas: cadastrar, editar, consultar, inativar/reativar      | Gerente  |
| UC07 | Tarifas: tolerância, fração, 1ª hora, fração adicional e diária por categoria | Gerente  |
| UC08 | Relatórios: por forma de pagamento, por dia, mensalistas × avulsos; imprimir/PDF e CSV (Excel) | Gerente |
| UC09 | Histórico com filtros (placa, período, operador, situação)         | Gerente  |
| UC10 | Processar pagamento: gateway **simulado** (há opção "simular recusa" para demonstrar o fluxo 5a) | Sistema |
| UC11 | Cálculo de tarifa automático (tolerância → 1ª hora → frações → teto da diária; mensalista com plano ativo não paga) | Sistema |

Fluxos alternativos da especificação (pátio lotado, placa inválida, ticket não encontrado,
pagamento recusado, CPF/placa duplicados, valores negativos, relatório/histórico vazio) estão tratados.

## Regras de negócio usadas (ajustáveis na tela de Tarifas)
- Padrão: tolerância 10 min, fração de 15 min.
- Carro: R$ 8,00 na 1ª hora + R$ 2,00 por fração, diária R$ 40,00. Moto: R$ 5,00 + R$ 1,00, diária R$ 20,00.
- 18 vagas de carro (A01–A03 reservadas para mensalistas, A18 bloqueada) e 6 de moto (M06 bloqueada).

## Estrutura
- `app.py` — servidor, API REST e regras de negócio (um único arquivo).
- `index.html` — interface (HTML + CSS + JS, um único arquivo).

## Fora do escopo (conforme o relatório)
Hardware (cancela/sensor), app mobile nativo, notificações e BI avançado. O perfil "Cliente Mensalista"
aparece no relatório, mas não tem caso de uso específico, por isso não foi criado.
