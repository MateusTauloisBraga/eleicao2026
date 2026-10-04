# Eleição 2026 · painel de apuração

Aplicação Django local para acompanhar a totalização do TSE por UF, com mapa de liderança, tendência e projeção nacional ponderada pelo eleitorado de cada estado.

## Executar no Windows / PowerShell

    py -m venv .venv
    .\.venv\Scripts\Activate.ps1
    python -m pip install -r requirements.txt
    python manage.py migrate
    python manage.py runserver

Abra http://127.0.0.1:8000. O painel usa exclusivamente os arquivos oficiais do TSE; antes dos primeiros boletins, mostra zero votos e aguarda atualização.

 O Django lê a configuração EA11 em https://resultados.tse.jus.br/oficial/comum/config/ele-c.json, identifica o código da eleição e baixa arquivos EA20 por UF. Em produção, o primeiro pedido após 10 segundos atualiza um snapshot compartilhado via Redis; os demais visitantes recebem o mesmo resultado. Cada atualização presidencial lê 27 arquivos estaduais mais um arquivo Brasil usado como sonda. O app não contorna erros ou limites do TSE; UFs indisponíveis são estimadas com referência regional/nacional e incerteza ampliada. A divulgação de resultados começa no horário definido pelo TSE.

O recorte de candidatos/cores é configurado em dashboard/services.py:
- Lula e Patrus: vermelho.
- Flávio Bolsonaro e Cleitinho: azul.
- Kalil: verde.

As correspondências de nomes podem precisar de ajuste quando o TSE publicar os nomes oficiais definitivos. O segundo turno só retorna dados quando existir configuração e arquivos para esse turno.

## Modelo

O nowcast ajusta uma regressão linear robusta por candidato/UF, com pesos maiores para observações recentes e estáveis, rejeita oscilações extremas via perdas de Huber, retrai inclinações estaduais para a tendência agregada e projeta o restante do eleitorado. A agregação nacional pondera o eleitorado elegível e a participação observada/estimada por UF. UFs sem boletins usam a referência regional; quando ainda não há região observada, usam a média das UFs disponíveis. O intervalo de 90% e a probabilidade de liderança vêm de simulação Monte Carlo com choques locais e nacionais. Essas probabilidades são exploratórias e **não estão calibradas**; valide-as com backtests antes de tratá-las como probabilidades eleitorais.

A tabela de eleitorado auxiliar está em dashboard/data/electorate.json e é uma aproximação usada quando o arquivo oficial ainda não informa o eleitorado; quando o TSE publica arquivos EA20, o serviço usa e.te como eleitorado oficial. A simplificação dos limites estaduais está no GeoJSON de dashboard/static/dashboard/states.geojson, derivado do dataset público [Brazil States GeoJSON (click_that_hood)](https://github.com/codeforgermany/click_that_hood/blob/main/public/data/brazil-states.geojson).

## Estrutura
- config/: configuração do projeto Django.
- dashboard/services.py: integração EA11/EA20 e modelo de projeção.
- dashboard/templates/dashboard/home.html: painel.
- dashboard/static/dashboard/: mapa, gráfico e estilos.
- dashboard/data/electorate.json: referência de eleitorado para arquivos ainda indisponíveis.

## Deploy no Render

O arquivo `render.yaml` descreve o serviço Django e um Redis Key Value compartilhado. Para publicar:

1. Envie somente esta pasta para um repositório Git privado.
2. No Render, escolha **New > Blueprint** e conecte o repositório.
3. Revise os planos Free do serviço web e do Key Value e os limites de uso da sua conta.
4. Clique em **Apply** para criar os recursos e aguarde o build/deploy.
5. Abra a URL `onrender.com` gerada e confirme que a aba de pesquisas e o painel oficial carregam.
6. Antes dos primeiros boletins, confira a mensagem de espera e a atualização automática.

O `render.yaml` gera `SECRET_KEY`, desliga `DEBUG`, conecta `REDIS_URL`, roda `collectstatic`/migrações e inicia Gunicorn com um worker. O Redis coordena o snapshot único entre visitantes/processos. O SQLite deste app só contém tabelas internas do Django; não é usado para armazenar boletins de apuração. Para domínio próprio, adicione-o ao serviço no painel e configure os registros DNS indicados pelo Render.
