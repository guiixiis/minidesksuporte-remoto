# MiniDesk v2 – acesso remoto (Python)

Três peças:
- `server.py` – relay (precisa estar acessível pela internet)
- `host.py`   – roda no PC que será controlado (tem janela com ID/senha)
- `viewer.py` – roda no PC de onde você controla (tem tela de login)

## Instalar (nos dois PCs)
    python -m pip install -r requirements.txt
(Linux: `sudo apt install python3-tk xclip`)

## Usar
1. Servidor:            python server.py
   Sem VPS, exponha com: ngrok http 8765   (use o endereço wss://...)
2. PC a ser acessado:   python host.py --server wss://SEU-ENDERECO
3. PC que controla:     python viewer.py      (preencha servidor, ID e senha)
4. No host, escolha o nível de acesso na janela que aparece.

## Novidades da v2
- Envia só as partes da tela que mudaram (muito mais fluido e leve)
- Qualidade automática conforme a rede + presets: Rápido / Equilibrado / Alta
- Vários monitores, tela cheia (F11), barra com FPS, KB/s e ping
- Transferência de arquivos (viewer -> host, salva em Downloads/MiniDesk)
- Área de transferência compartilhada (texto)
- Modo "somente visualizar" e níveis de permissão escolhidos no host
- Janela do host com botão "Desconectar" e "Nova senha"
- Login gráfico no viewer (lembra servidor e ID)

## Segurança
- Senha nunca trafega (desafio/resposta HMAC); aprovação manual a cada conexão
- Arquivos e área de transferência só com sua permissão explícita
- Use sempre wss:// (TLS) para criptografar tela e comandos
- --auto-accept (acesso sem aprovação) só com --password forte
