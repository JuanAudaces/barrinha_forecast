---
name: calibrar
description: Registra uma sessão de surf na Praia da Barrinha a partir do relato do usuário (dia, hora, tamanho, como estava) e roda a calibração da previsão. Use quando o usuário contar como estava o mar num dia em que foi à praia, pedir para registrar/anotar uma sessão, ou invocar /calibrar.
---

# Calibrar a previsão com uma sessão

O usuário conta, em português livre, como estava a Barrinha num dia em que surfou. Você transforma o relato em um registro no diário (`sessoes.csv`), roda a calibração e devolve um resumo curto. Tudo roda a partir da raiz do projeto (onde está `surf.py`), com `PYTHONIOENCODING=utf-8` para os acentos saírem certos no terminal.

## 1. Extrair do relato

| Campo | Como obter |
|---|---|
| **Data e hora** | Converta datas relativas ("domingo", "ontem", "sábado passado") usando a data de hoje. Arredonde a hora para a hora cheia mais próxima ("umas 15h30" → 16; "de manhã cedo" → pergunte ou use 7 e diga que assumiu). |
| **Altura (m)** | Use o número que ele disser. Se vier em linguagem de surfista, converta: joelho 0.5 · cintura 0.7 · peito 1.0 · ombro 1.2 · cabeça 1.5 · meio metro acima da cabeça 2.0 · "gigante"/"dois metros" 2.0+. É a altura da **série na Barrinha**, não a do mar aberto. |
| **Nota (1–5)** | Nota **para o nível dele**: intermediário, com pouco condicionamento para varar a arrebentação. Mar grande e pesado pode ser nota baixa mesmo bonito. Se ele não der a nota, proponha uma com base no relato e peça para confirmar. |
| **Pontos do mapa** | `--levanta` = onde a onda levanta/quebra primeiro; `--morre` = onde perde força ou fecha. Leia os ids e nomes atuais em `mapa/pontos.json` (hoje: 1 Ponta do Molhe, 2 Pico do Meio, 3 Canto do Molhe, 4 Inside/beirinha, 5 Banco Norte, 6 Bolha, atrás do molhe). Só preencha se o relato indicar o lugar; não chute. |
| **Obs** | Resumo curto do relato nas palavras dele: força, se fechava, vento, maré, crowd, correnteza. |

**Obrigatórios:** data, hora, altura e nota. Se faltar algum, pergunte só o que falta, numa pergunta só, antes de registrar. Os outros campos são opcionais.

## 2. Registrar

```
PYTHONIOENCODING=utf-8 python surf.py log <nota> <altura> "AAAA-MM-DD HH" "<obs>" [--levanta N] [--morre N]
```

- Se responder `Já existe sessão em ...`, mostre ao usuário o que está registrado (`sessoes.csv`) e pergunte se deve trocar. Só com o "sim" dele, repita o comando com `--substituir`.
- Se ele contar várias sessões de uma vez, registre todas antes de calibrar.
- Não edite `sessoes.csv` na mão; use sempre o comando.

## 3. Calibrar e atualizar a página

```
PYTHONIOENCODING=utf-8 python surf.py calibrar
PYTHONIOENCODING=utf-8 python surf.py
```

O segundo comando regenera `previsao.html` com a calibração nova e abre no navegador.

Depois publique, para o site (GitHub Pages, que o celular da parede mostra) usar a calibração nova:

```
git add sessoes.csv calibracao.json && git commit -m "Sessão AAAA-MM-DD HHh" && git push
```

O push dispara o workflow `previsao`, que regenera e publica a página em ~1 min.

## 4. Responder

Curto, em português, sem despejar a saída do terminal:

- O que foi registrado: data e hora, altura, nota e pontos.
- **Previsto × real**: quanto os modelos previam naquela hora e quanto deu na Barrinha (a saída do `calibrar` mostra o erro médio por modelo e a razão real÷prevista por direção do swell).
- O que mudou: qual modelo está acertando mais e se algum fator de direção passou a valer. Os fatores de direção precisam de 2+ sessões naquela direção, e os pesos dos modelos precisam de 3+ sessões; diga quantas faltam.
- Se algo parecer estranho (por exemplo, real 3× maior que o previsto), comente como hipótese e sugira o que observar na próxima sessão.
