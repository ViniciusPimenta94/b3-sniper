# Preços de referência

O monitoramento de preços lê `data/referencias_precos.json`. `config.json` continua com Telegram, agenda e o cadastro anterior, usado só como rollback. `ordens.json` não entra neste cálculo.

Cada ativo pode ter três referências independentes:

- **Preço justo:** Graham, Bazin, manual ou valor patrimonial do FII.
- **Preço teto:** desconto sobre o preço justo, ou máxima de 52 semanas.
- **Faixa:** mínimo e máximo informados.

O preço justo continua calculado e aparece no `/status`, mas não gera alerta de compra. Para quem tem preço justo, o alerta de compra sai só quando o preço teto é válido e o desconto é maior que zero, com o preço igual ou abaixo do teto. A faixa dispara quando o preço está dentro do intervalo. Desconto `0` não gera alerta de compra.

## Preenchimento

Percentuais são fração: `0,08` = 8% e `0,05` = 5%.

| Método | Onde | Campos |
|---|---|---|
| `graham` | `preco_justo` | `lpa`, `vpa`, `origem`, `data_fundamentos` |
| `bazin` | `preco_justo` | `dividendos_12m`, `yield_alvo`, `origem`, `data_fundamentos` |
| `manual` | `preco_justo` | `preco_manual`, `origem`, `data_fundamentos` |
| `fii_vp` | `preco_justo` | `vpa`, `origem`, `data_fundamentos` |
| `desconto_sobre_justo` | `preco_teto` | `desconto_pct` (exige preço justo válido) |
| `max_52_semanas` | `preco_teto` | `desconto_pct`, `maxima_52s`, `data_cotacao`, `origem` |
| faixa | `faixa` | `preco_minimo`, `preco_maximo` |

`data_fundamentos` e `data_cotacao` usam `AAAA-MM-DD`. `validade_dias` vale para as duas. `margem_proximidade_pct` é percentual: `5` amplia o gatilho em 5%. A faixa não usa essa margem.

Fórmulas:

- Graham: `√(22,5 × LPA × VPA)`
- Bazin: `dividendos 12 meses / yield alvo`
- Manual: preço informado
- FII: valor patrimonial por cota
- Teto por desconto: `preço justo × (1 − desconto)`
- Máxima de 52 semanas: `máxima × (1 − desconto)`

## Validação

Não há alerta de preço quando falta origem, a data está ausente, inválida, no futuro ou vencida, ou quando LPA, VPA, dividendos, yield, desconto ou faixa são inválidos. O bot envia um aviso único por pendência e só volta a alertar o preço depois que o cadastro passa na validação.

O `/status` mostra o valor quando os números já permitem calculá-lo, mesmo com a data ausente, inválida ou vencida, e identifica a pendência. Esse valor suspenso não dispara alerta.

Yield e desconto ficam no intervalo `[0, 1)`. Yield `0` é rejeitado. Preços e fundamentos numéricos precisam ser finitos e maiores que zero.

VISC11, XPLG11, PVBI11, BTHF11 e CPTS11 têm o VPA preenchido, origem `Status Invest` e data de consulta `2026-10-16` em `data_fundamentos`. Essa data é a da consulta informada; o arquivo não registra a competência contábil do VPA. IVVB11, WRLD11 e WEGE3 têm a máxima de 52 semanas preenchida, origem `Status Invest` e data de cotação `2026-10-16` em `data_cotacao`. O desconto desses três ativos permanece `0,05`.

## Atualização

Fundamentos de Graham, Bazin, manual e FII são editados no JSON. O script não substitui esses números por dados do Yahoo.

`python atualizar_precos.py` só preenche a máxima de 52 semanas e a data da coleta. O desconto configurado é mantido. Se a coleta falhar, o teto anterior permanece e nenhum alerta passa a usar um valor inventado.

## Alertas

O preço teto é o limite de compra. A faixa continua independente. As ordens de `ordens.json` também continuam independentes deste cálculo.

- BBSE3 e TIMS3 usam Bazin com desconto de `0,15`. O alerta de compra fica em 85% do preço justo. TIMS3 tem yield `0,06` e dividendos `1,4723`. BBSE3 tem yield `0,08` e dividendos `4,5901`.
- ITUB4, TAEE11, TOTS3, SBSP3 e CMIG4 usam Graham com desconto de `0,25`. O alerta de compra fica em 75% do preço justo. TAEE11 tem LPA `4,72` e VPA `23,43`. Itaú, TOTVS, Sabesp e Cemig usam os fundamentos de 14/10/2026.
- Os cinco FIIs permanecem com desconto `0` até a classificação de cada um. Com esse desconto, não há alerta de compra pelo preço justo.
- WEG, IVVB11 e WRLD11 usam a máxima de 52 semanas, com desconto `0,05`. As máximas registradas são IVVB11 `458,10`, WRLD11 `153,37` e WEGE3 `53,83`, com origem `Status Invest` e data da cotação `2026-10-16`.
- Os FIIs usam `fii_vp`. Os VPAs registrados são VISC11 `115,60`, XPLG11 `105,03`, PVBI11 `104,53`, BTHF11 `9,86` e CPTS11 `8,65`, com origem `Status Invest` e data de consulta `2026-10-16`.
- As faixas de B3, Cyrela, C&A, Banco do Brasil e Petrobras foram copiadas sem mudança.

## Deduplicação

O episódio ativo fica em memória e em `data/alertas_estado.json`. O mesmo gatilho não é reenviado enquanto o preço permanecer na condição. Ao sair, a chave é removida e uma nova entrada pode alertar de novo. O preço justo não entra nesse alerta de compra.

A chave da pendência guarda a natureza do problema e omite a idade em dias. A mesma pendência continua deduplicada no dia seguinte. Uma pendência de outra natureza, como data ausente que passa a ser LPA inválido, gera um aviso novo. Uma chave gravada antes desta correção, ainda com a idade no texto, não coincide com a chave nova: o aviso daquela pendência pode sair uma vez depois da atualização.

O arquivo de estado não deve ser versionado. No Render o disco é efêmero: a deduplicação sobrevive a um restart só se o arquivo persistir no ambiente.

## Rollback

`config.json` e `ordens.json` não foram recalculados. Para voltar ao comportamento anterior, reverta `bot_mercado.py`, `atualizar_precos.py` e `referencias.py` para o commit anterior e apague, se quiser, `data/referencias_precos.json` e `data/alertas_estado.json`. O código antigo não lê esses arquivos. Não é necessário restaurar preços no `config.json`, porque eles não foram alterados.
