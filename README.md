# Eleição 2026 · painel de apuração

Aplicação Django local para acompanhar a totalização do TSE por UF, com mapa de liderança, tendência e projeção nacional ponderada pelo eleitorado de cada estado.

## Executar no Windows / PowerShell

    py -m venv .venv
    .\.venv\Scripts\Activate.ps1
    python -m pip install -r requirements.txt
    python manage.py migrate
    python manage.py runserver

Abra http://127.0.0.1:8000. O painel usa exclusivamente os arquivos oficiais do TSE; antes dos primeiros boletins, mostra zero votos e aguarda atualização.

O Django lê a configuração EA11 em https://resultados.tse.jus.br/oficial/comum/config/ele-c.json, identifica o código da eleição e baixa arquivos EA20 por UF. Em produção, o primeiro pedido após 10 segundos atualiza um snapshot compartilhado via Redis; os demais visitantes recebem o mesmo resultado. Cada ponto novo é gravado no Postgres e recuperado dele caso o Redis reinicie. Cada atualização presidencial lê 27 arquivos estaduais, o arquivo separado do exterior (`ZZ`) e o agregado Brasil (`BR`). O agregado `BR` fornece os votos observados oficiais e confere a soma das 28 localidades; nunca é somado a elas. A projeção territorial usa os arquivos locais e avisa quando a soma não concilia com `BR`, sem pressupor a causa da diferença. O app não contorna erros ou limites do TSE; localidades indisponíveis são estimadas com referência regional/nacional e incerteza ampliada. A divulgação de resultados começa no horário definido pelo TSE.

O recorte de candidatos/cores é configurado em dashboard/services.py:
- Lula e Patrus: vermelho.
- Flávio Bolsonaro e Cleitinho: azul.
- Kalil: verde.

As correspondências de nomes podem precisar de ajuste quando o TSE publicar os nomes oficiais definitivos. O segundo turno só retorna dados quando existir configuração e arquivos para esse turno. Para governador de MG, o painel exibe o primeiro turno: Cleitinho Azevedo foi eleito nele e não há apuração estadual de segundo turno.

## Modelo

O nowcast ajusta uma regressão linear robusta por candidato/UF. A inclinação só é usada após pelo menos três observações, 5 pontos percentuais de avanço local e 15% de cobertura da UF; sua magnitude é limitada. A agregação nacional pondera o eleitorado e a participação observada/estimada. UFs sem boletins usam referência regional ou nacional. O intervalo exploratório usa simulação Monte Carlo com choques locais, regionais e nacionais. A projeção presidencial começa com os primeiros votos nacionais e locais, sem corte mínimo de cobertura; diferenças entre o total nacional e a soma das localidades são sinalizadas e a estimativa inicial pode mudar bastante. Não exibimos probabilidade de vitória porque o modelo ainda não foi calibrado por backtests para esse cenário.

O indicador de vitória matemática é independente da projeção. Usa votos válidos oficiais, eleitorado total e eleitorado das seções já totalizadas no arquivo do TSE. No segundo turno, os votos adicionais necessários para garantir a vitória são `max(0, floor((votos_do_adversário + eleitores_pendentes - votos_do_candidato)/2) + 1)`: assume, conservadoramente, que cada eleitor pendente pode votar validamente no adversário. No primeiro turno, a garantia exige maioria absoluta mesmo se todos os eleitores pendentes votarem validamente. O painel distingue esse cálculo da proclamação formal do TSE.

A tabela de eleitorado auxiliar está em dashboard/data/electorate.json e é uma aproximação usada quando o arquivo oficial ainda não informa o eleitorado; quando o TSE publica arquivos EA20, o serviço usa e.te como eleitorado oficial. A simplificação dos limites estaduais está no GeoJSON de dashboard/static/dashboard/states.geojson, derivado do dataset público [Brazil States GeoJSON (click_that_hood)](https://github.com/codeforgermany/click_that_hood/blob/main/public/data/brazil-states.geojson).

## Estrutura
- config/: configuração do projeto Django.
- dashboard/services.py: integração EA11/EA20 e modelo de projeção.
- dashboard/templates/dashboard/home.html: painel.
- dashboard/static/dashboard/: mapa, gráfico e estilos.
- dashboard/data/electorate.json: referência de eleitorado para arquivos ainda indisponíveis.

## Deploy no Render

O arquivo `render.yaml` descreve o serviço Django, um Redis Key Value compartilhado e um Postgres Free para o histórico. Para publicar:

1. Envie somente esta pasta para um repositório Git privado.
2. No Render, escolha **New > Blueprint** e conecte o repositório.
3. Revise os planos Free do serviço web, do Key Value e do Postgres e os limites de uso da sua conta.
4. Clique em **Apply** para criar os recursos e aguarde o build/deploy.
5. Abra a URL `onrender.com` gerada e confirme que a aba de pesquisas e o painel oficial carregam.
6. Antes dos primeiros boletins, confira a mensagem de espera e a atualização automática.

O `render.yaml` gera `SECRET_KEY`, desliga `DEBUG`, conecta `REDIS_URL` e `DATABASE_URL`, roda `collectstatic`/migrações e inicia Gunicorn com um worker. O Redis coordena o snapshot único entre visitantes/processos; o Postgres guarda os pontos do gráfico. Localmente, sem `DATABASE_URL`, o app usa SQLite. O Postgres Free do Render expira 30 dias após a criação. Para domínio próprio, adicione-o ao serviço no painel e configure os registros DNS indicados pelo Render.
