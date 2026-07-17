// Proxy seguro: a chave da Anthropic fica SÓ aqui no servidor (variável de
// ambiente ANTHROPIC_API_KEY). O navegador nunca vê a chave.
module.exports = async (req, res) => {
  if (req.method !== 'POST') {
    return res.status(405).json({ error: { message: 'use POST' } });
  }
  if (!process.env.ANTHROPIC_API_KEY) {
    return res.status(500).json({ error: { message: 'ANTHROPIC_API_KEY não configurada no servidor' } });
  }

  const { system, messages } = req.body || {};

  // validação básica — evita abuso do endpoint
  if (!Array.isArray(messages) || messages.length === 0 || messages.length > 30) {
    return res.status(400).json({ error: { message: 'messages inválido' } });
  }
  for (const m of messages) {
    if (!m || (m.role !== 'user' && m.role !== 'assistant') ||
        typeof m.content !== 'string' || m.content.length > 2000) {
      return res.status(400).json({ error: { message: 'mensagem malformada' } });
    }
  }

  try {
    const r = await fetch('https://api.anthropic.com/v1/messages', {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'x-api-key': process.env.ANTHROPIC_API_KEY,
        'anthropic-version': '2023-06-01'
      },
      body: JSON.stringify({
        model: 'claude-sonnet-4-6',
        max_tokens: 600,                       // teto do servidor, controla custo
        system: String(system || '').slice(0, 4000),
        messages
      })
    });
    const data = await r.json();
    return res.status(r.status).json(data);
  } catch (e) {
    return res.status(502).json({ error: { message: 'falha ao falar com a Anthropic: ' + e.message } });
  }
};
