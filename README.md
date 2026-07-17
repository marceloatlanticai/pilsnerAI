# Lúpula — deploy no Vercel (grátis)

Retrato de pontilhismo vivo que bebe pilsner e opina sobre suas ideias.
A chave da API fica protegida no servidor — nunca aparece no navegador.

## Estrutura

```
index.html      ← o app inteiro (canvas + chat)
api/chat.js     ← proxy serverless que guarda a chave
```

## Passo a passo (5 passos)

1. **Suba esta pasta no GitHub.** Crie um repositório novo e envie os
   arquivos exatamente nesta estrutura (a pasta `api/` na raiz é obrigatória
   — é ela que o Vercel transforma em função serverless).

2. **Crie uma conta no [vercel.com](https://vercel.com)** usando o login do
   GitHub (plano Hobby, grátis).

3. **Importe o repositório.** No painel do Vercel: *Add New → Project →*
   selecione o repositório *→ Deploy*. Não precisa configurar build — é
   site estático + função.

4. **Adicione a chave.** No projeto: *Settings → Environment Variables →*
   crie `ANTHROPIC_API_KEY` com a sua chave (obtida em
   [console.anthropic.com](https://console.anthropic.com/settings/keys)).
   Depois vá em *Deployments* e clique em **Redeploy** para a variável valer.

5. **Pronto.** Abra a URL `https://seu-projeto.vercel.app`, clique uma vez
   na página (libera o áudio) e converse com a Lúpula.

## Como o front acha a API

O `index.html` tenta primeiro `/api/chat` (o proxy). Se não existir — como
dentro do claude.ai — cai automaticamente na API direta da Anthropic, que
lá é autenticada pela plataforma. Um único arquivo funciona nos dois mundos.

## Custos e limites

- Vercel Hobby: grátis para esse volume.
- Anthropic: paga por uso; o proxy limita `max_tokens` a 600 e o histórico
  a 30 mensagens para segurar o custo.
- Se quiser trancar mais, adicione rate-limit por IP no `api/chat.js`.

## Netlify / Cloudflare

Mesma lógica: mova `api/chat.js` para o formato de função de cada
plataforma (`netlify/functions/chat.js` ou Worker) e mantenha o caminho
público `/api/chat` — o front não precisa mudar.
