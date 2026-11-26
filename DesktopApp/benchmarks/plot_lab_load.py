"""Publish real lab measurements; never substitute synthetic data silently."""
import argparse
from collections import defaultdict
import csv
import json
from pathlib import Path
import shutil
from statistics import median,mean
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import MultipleLocator


def percentile(values,fraction):
    values=sorted(values)
    position=(len(values)-1)*fraction
    lower=int(position)
    return values[lower]+(values[min(lower+1,len(values)-1)]-values[lower])*(position-lower)


def main():
    parser=argparse.ArgumentParser(__doc__)
    parser.add_argument('run',type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--allow-synthetic',action='store_true',help='Instrumentation validation only, prominently labelled.')
    args=parser.parse_args()
    root,out=args.run,args.output
    assert (root/'COMPLETE').exists() and not (root/'FAILED.json').exists()
    env=json.loads((root/'environment.json').read_text(encoding='utf-8'))
    synthetic=env['kind']!='real-lab'
    if synthetic and not args.allow_synthetic:raise ValueError('Real lab graphs require real lab data.')
    out.mkdir(parents=True,exist_ok=False)
    with (root/'measurements.csv').open(encoding='utf-8') as handle:
        rows=[{k:v if k=='mode' else float(v) for k,v in row.items()} for row in csv.DictReader(handle)]
    http=[json.loads(line) for line in (root/'http.jsonl').read_text(encoding='utf-8').splitlines()]
    rates,counts=env['rates'],env['threads']
    groups=defaultdict(list)
    for row in rows:groups[row['mode'],int(row['rate']),int(row['threads'])].append(row)
    assert set(groups)=={(m,r,n) for m in ('proxy','https') for r in rates for n in counts}
    assert all(len(group)==env['repetitions'] for group in groups.values())
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':11,'svg.fonttype':'none'})
    colors=dict(zip(rates,plt.cm.viridis([i/max(1,len(rates)-1) for i in range(len(rates))])))
    title='ПРОВЕРКА ИЗМЕРИТЕЛЯ — НЕ LAB' if synthetic else 'Лабораторный кластер lab1'
    for mode in ('proxy','https'):
        fig,axes=plt.subplots(1,2,figsize=(14,5.4))
        for rate in rates:
            selected=[groups[mode,rate,n] for n in counts]
            values=[median(r['total_seconds'] for r in group) for group in selected]
            axes[0].plot(counts,values,'o-',label=f'{rate} запросов/с',color=colors[rate])
            axes[0].fill_between(counts,[min(r['total_seconds'] for r in group) for group in selected],
                [max(r['total_seconds'] for r in group) for group in selected],alpha=.12,color=colors[rate])
            axes[1].plot(counts,[median(r['api_mean_ms'] for r in group) for group in selected],'o-',color=colors[rate],label=f'{rate} запросов/с')
        for ax in axes:
            ax.set_xlabel('Потоков на кластер');ax.set_xticks(counts);ax.set_ylim(bottom=0);ax.grid(alpha=.22)
            ax.spines[['top','right']].set_visible(False)
            ax.legend(fontsize=9)
        axes[0].set(title='Полная загрузка и отрисовка матрицы',ylabel='Время, с')
        axes[1].set(title='Средний HTTP-ответ без очереди клиента',ylabel='Время, мс')
        fig.suptitle(f'{title} · {"kubectl proxy" if mode=="proxy" else "HTTPS"}',fontweight='bold')
        fig.text(.5,.025,'200 Pod · 10 namespace × 5 Deployment × 4 Pod · Qt offscreen · меньше — быстрее.\nЛиния — медиана повторов; заливка — их минимум/максимум. Частота — заданный предел, фактическая записана в CSV.',ha='center',fontsize=10)
        fig.tight_layout(rect=(0,.10,1,.93))
        for ext in ('png','svg'):fig.savefig(out/f'{mode}-matrix.{ext}',dpi=160)
        plt.close(fig)
    fig,axes=plt.subplots(1,2,figsize=(13,5.1))
    latency=[]
    for mode,color in (('proxy','#3079bb'),('https','#d16a25')):
        medians,p95,achieved=[],[],[]
        for rate in rates:
            physical=[r for r in http if r['mode']==mode and r['rate']==rate]
            response=[r['response_ms'] for r in physical]
            queue=[r['queue_ms'] for r in physical]
            trials=[r for r in rows if r['mode']==mode and r['rate']==rate]
            actual=sum(r['requests'] for r in trials)/sum(r['load_seconds'] for r in trials)
            medians.append(median(response));p95.append(percentile(response,.95));achieved.append(actual)
            latency.append(dict(mode=mode,rate=rate,samples=len(physical),response_p50_ms=median(response),
                response_p95_ms=percentile(response,.95),response_mean_ms=mean(response),queue_p50_ms=median(queue),actual_rps=actual))
        axes[0].plot(rates,medians,'o-',color=color,label=mode+' p50')
        axes[0].plot(rates,p95,'o--',color=color,label=mode+' p95')
        axes[1].plot(rates,achieved,'o-',color=color,label=mode)
    axes[1].plot(rates,rates,':',color='gray',label='Заданный предел')
    for ax in axes:
        ax.set_xticks(rates);ax.set_xlabel('Заданный предел, запросов/с');ax.set_ylim(bottom=0);ax.grid(alpha=.22);ax.legend(fontsize=9)
    axes[0].set(title='Приблизительное время ответа API',ylabel='HTTP-ответ, мс')
    axes[1].set(title='Достигнутая частота во время загрузки',ylabel='Запросов/с')
    fig.suptitle(title+' · отклик API и достигнутая нагрузка',fontweight='bold')
    fig.tight_layout(rect=(0,0,1,.93))
    for ext in ('png','svg'):fig.savefig(out/f'api-response.{ext}',dpi=160)
    plt.close(fig)
    for filename in ('measurements.csv','http.jsonl','environment.json','matrix-proxy.png','matrix-https.png'):
        if (root/filename).exists():shutil.copyfile(root/filename,out/filename)
    (out/'response-summary.json').write_text(json.dumps(latency,indent=2),encoding='utf-8')
    lines=['# Загрузка матрицы и отклик лабораторного API','',title,'',
        f'Измерено {len(rows)} полных загрузок: {len(counts)} значений потоков × {len(rates)} частот × 2 транспорта × {env["repetitions"]} повтора.','',
        '![Proxy](proxy-matrix.png)','','![HTTPS](https-matrix.png)','','![Отклик API](api-response.png)','',
        '## Как читать результаты','',
        '- Матрица: 10 namespace, в каждом 5 Deployment с четырьмя Pod. Пять имён приложений общие: 50 ячеек и 200 маркеров. В реальном кластере фоновые системные ресурсы сохраняются и тоже попадают в общие LIST; число физических запросов включает пагинацию.',
        f'- Потоки: {", ".join(map(str,counts))}. Частоты: {", ".join(map(str,rates))} запросов/с на кластер. Настройка применяется к RequestPolicy и EndpointBudget; начальный запас всегда 2. Состояние лимитера между обновлениями сохраняется — это продолжительная нагрузка, а не серия свежих стартовых burst.',
        '- В первом повторе потоки идут по возрастанию, во втором — по убыванию. Порядок транспортов чередуется. Перед каждой частотой есть прогрев; кэш очищается перед каждым обновлением.',
        '- Время матрицы включает сбор всех списков, создание модели, processEvents, repaint и grab().toImage(). Qt offscreen исключает Windows DWM/GPU/RDP. Промежуточный вывод частей матрицы исключён.',
        '- HTTP-ответ измеряется от физического начала запроса до окончания чтения тела. Ожидание допуска лимитера и последующий разбор JSON исключены. Сеть, TLS и локальный proxy входят в это приближение; это не чистое серверное время обработки.',
        '- p50 — медиана HTTP-запросов, p95 — значение, быстрее которого завершились 95% измеренных запросов. Смешаны разные типы LIST; пути каждого запроса и его длительность сохранены в http.jsonl.',
        '- Фактическая частота может быть ниже заданного предела из-за ответов сервера, последовательного чтения namespace, отрисовки и ограниченного числа независимых списков. По одной настройке QPS нельзя утверждать, что кластер перегружен.',
        '- В режиме реального lab создаются отдельные namespace с меткой запуска; очистка использует UID и подтверждает их отсутствие. Существующие ресурсы не изменяются. Отдельные CPU/RAM stress-контейнеры не запускаются.','',
        '## Приблизительный отклик API','',
        '| Режим | Предел, запросов/с | Достигнуто, запросов/с | HTTP-запросов | p50, мс | p95, мс | Среднее, мс | Медиана ожидания допуска, мс |',
        '|---|---:|---:|---:|---:|---:|---:|---:|']
    for r in latency:
        lines.append(f"| {r['mode']} | {r['rate']} | {r['actual_rps']:.2f} | {r['samples']} | {r['response_p50_ms']:.1f} | {r['response_p95_ms']:.1f} | {r['response_mean_ms']:.1f} | {r['queue_p50_ms']:.1f} |")
    lines+=['','## Полное время матрицы: медианы, секунды','',
        '| Потоков | '+' | '.join(f'{m} {r}/с' for m in ('proxy','https') for r in rates)+' |',
        '|---:|'+'---:|'*(2*len(rates))]
    for n in counts:
        lines.append('| '+str(n)+' | '+' | '.join(f"{median(row['total_seconds'] for row in groups[m,r,n]):.3f}" for m in ('proxy','https') for r in rates)+' |')
    lines+=['','[Исходные измерения](measurements.csv) · [HTTP-запросы](http.jsonl) · [Условия и SHA-256 исходников](environment.json)','',
        'При высокой нагрузке Kubernetes может ставить запросы в очередь и отклонять их; это описано в '
        '[API Priority and Fairness](https://kubernetes.io/docs/concepts/cluster-administration/flow-control/). '
        'Наличие перегрузки оценивается по измеренному отклику и отказам, а не только по числу потоков.','']
    (out/'REPORT.md').write_text('\n'.join(lines),encoding='utf-8')


if __name__=='__main__':main()
