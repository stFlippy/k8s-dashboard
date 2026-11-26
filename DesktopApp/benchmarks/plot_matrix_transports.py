"""Plot every measured thread setting; run in the isolated matplotlib environment."""
import argparse
from collections import defaultdict
import csv
import json
from pathlib import Path
from statistics import median
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import MultipleLocator


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    root = args.directory
    assert (root/'COMPLETE').exists(), 'Only complete measurements may be plotted.'
    environment = json.loads((root/'environment.json').read_text(encoding='utf-8'))
    assert environment['repetitions'] == 2 and environment['server_delay_ms'] == 50, 'Use the documented default protocol.'
    with (root/'measurements.csv').open(encoding='utf-8') as handle:
        rows = [{k: (v if k=='mode' else float(v)) for k,v in row.items()} for row in csv.DictReader(handle)]
    groups = defaultdict(list)
    for row in rows: groups[row['mode'],int(row['threads'])].append(row)
    assert set(groups) == {(m,n) for m in ('proxy','https') for n in range(51)}
    assert all(len(group) == 2 for group in groups.values())
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':11,'svg.fonttype':'none'})
    colors = {'proxy':'#3079bb','https':'#d16a25'}
    labels = {'proxy':'kubectl proxy','https':'Прямой HTTPS'}
    def series(mode, field, statistic=median):
        return [statistic(r[field] for r in groups[mode,n]) for n in range(51)]
    def plot(ax, mode, field, title, ylabel):
        xs = list(range(51))
        ax.fill_between(xs,series(mode,field,min),series(mode,field,max),color=colors[mode],alpha=.14,label='Разброс двух замеров')
        ax.plot(xs,series(mode,field),color=colors[mode],marker='o',markersize=3,linewidth=1.4,label='Медиана двух замеров')
        ax.set(title=title,xlabel='Потоков на кластер',ylabel=ylabel,xlim=(-.5,50.5),ylim=(0,None))
        ax.xaxis.set_major_locator(MultipleLocator(5))
        ax.xaxis.set_minor_locator(MultipleLocator(1))
        ax.grid(axis='both',alpha=.23)
        ax.spines[['top','right']].set_visible(False)
        ax.legend(loc='best',fontsize=9)
    all_render = max(r['render_ms'] for r in rows)*1.1
    all_total = max(r['total_seconds'] for r in rows)*1.1
    for mode in ('proxy','https'):
        fig, axes = plt.subplots(1,2,figsize=(14,5.3))
        plot(axes[0],mode,'render_ms','Построение и отрисовка матрицы','Время, мс (меньше — быстрее)')
        plot(axes[1],mode,'total_seconds','Загрузка данных + отрисовка','Время, с (меньше — быстрее)')
        axes[0].set_ylim(0,all_render)
        axes[1].set_ylim(0,all_total)
        fig.suptitle(f'{labels[mode]} · 200 Pod · 10 namespace × 5 Deployment × 4 Pod',fontweight='bold',y=.98)
        fig.text(.5,.02,'0 — последовательный сбор без сетевого пула. 2 запроса/с, burst 2; 9 LIST; задержка стенда 50 мс.\nЛокальный TLS-стенд, Qt 5 offscreen; это не замер FPS или производственного кластера.',ha='center',fontsize=10)
        fig.tight_layout(rect=(0,.11,1,.92))
        for suffix in ('png','svg'): fig.savefig(root/f'{mode}-matrix.{suffix}',dpi=160)
        plt.close(fig)
    fig, axes = plt.subplots(1,2,figsize=(14,5.2))
    for mode in ('proxy','https'):
        for ax,field in zip(axes,('render_ms','total_seconds')):
            ax.plot(range(51),series(mode,field),color=colors[mode],marker='o',markersize=3,label=labels[mode])
    for ax,title,ylabel in zip(axes,('Построение и отрисовка','Загрузка + отрисовка'),('мс','с')):
        ax.set(title=title,xlabel='Потоков на кластер',ylabel=ylabel,xlim=(-.5,50.5),ylim=(0,None))
        ax.xaxis.set_major_locator(MultipleLocator(5))
        ax.xaxis.set_minor_locator(MultipleLocator(1))
        ax.grid(alpha=.23)
        ax.legend()
    fig.suptitle('Сравнение транспортов · 200 Pod · все значения 0–50, шаг 1',fontweight='bold')
    fig.text(.5,.025,'Медиана двух замеров. Один локальный TLS-стенд; 2 запроса/с; Qt 5 offscreen. Меньше — быстрее.',ha='center',fontsize=10)
    fig.tight_layout(rect=(0,.07,1,.94))
    for suffix in ('png','svg'): fig.savefig(root/f'comparison.{suffix}',dpi=160)
    plt.close(fig)
    summary = {mode:{str(n):{field:series(mode,field)[n] for field in ('load_seconds','render_ms','total_seconds','cpu_seconds')}
                     for n in range(51)} for mode in ('proxy','https')}
    (root/'medians.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    lines = ['# Замер матрицы: kubectl proxy и HTTPS','',
        'Все 102 точки измерены: 0–50 потоков с шагом 1 для каждого транспорта, по два повтора (204 запуска). '
        'Графики показывают медиану; заливка — минимум и максимум двух повторов, не доверительный интервал.','',
        '![kubectl proxy](proxy-matrix.png)','', '![HTTPS](https-matrix.png)','',
        '## Результаты','',
        '| Режим | Потоков | Загрузка, с | Построение/отрисовка, мс | Всего, с |',
        '|---|---:|---:|---:|---:|',
        *[f"| {labels[mode]} | {n} | {summary[mode][str(n)]['load_seconds']:.3f} | {summary[mode][str(n)]['render_ms']:.1f} | {summary[mode][str(n)]['total_seconds']:.3f} |"
          for mode in ('proxy','https') for n in (1,50)], '',
        f"Максимум одновременно активных HTTP-запросов на стенде во всех попытках: {int(max(r['server_peak'] for r in rows))}. "
        'При задержке 50 мс запрос заканчивается до следующего разрешённого лимитером. Рост числа потоков до 50 не дал устойчивого сокращения времени загрузки. '
        'Это не означает, что параллельность бесполезна при более медленном сервере или ином объёме данных.','',
        'Отрисовка выполняется в GUI-потоке и не распараллеливается настройкой сетевого пула. '
        'Числа потоков в обоих повторах проходились по возрастанию; возможен временной дрейф. '
        'Двух повторов недостаточно, чтобы трактовать небольшие различия режимов или отдельные пики как устойчивый эффект.','',
        '## Условия','',
        '- Один локальный TLS endpoint, настоящий внешний kubectl proxy и прямой HTTPS Quaber; синтетический kubeconfig. Реальные кластеры не изменялись.',
        '- 10 namespace, в каждом 5 Deployment с четырьмя Pod: 50 Deployment, 50 ReplicaSet, 200 Pod. Имена пяти приложений общие: матрица 5 строк × 10 столбцов, 50 ячеек, 200 маркеров.',
        '- Сервер добавляет 50 мс на запрос. Это заданная модель, а не воспроизведение сети production. Сохранены обычные лимиты 2 запроса/с и burst 2, включая EndpointBudget.',
        '- Каждая попытка получает пустой кэш и отдельное состояние лимитера с одинаковым стартовым burst. Процесс proxy и TLS-контексты прогреты вне измерений. Порядок транспортов чередуется.',
        '- 0: последовательный сбор в фоновом сборщике без выделенного I/O пула. 1–50: реальные значения configure_cluster_threads; максимумы пулов, а не обещание стольких активных HTTP запросов.',
        '- Загрузка: FetchPlan + collect_cluster, 9 физических LIST, без подстановки готового snapshot. Проверяются число запросов, отсутствие ошибок, 10 namespace, 200 Pod и 50 заполненных ячеек. Промежуточный вывод отдельных областей исключён: измеряется завершение всей матрицы.',
        '- Отрисовка: обычный MainWindow/MatrixPresenter, сброс структуры перед каждым замером, построение модели + processEvents + repaint + grab().toImage(). Окно 1450×900; все 200 маркеров видимы.',
        '- Qt 5 offscreen: программная отрисовка без DWM/GPU/RDP. Это длительность полного построения матрицы, не FPS. Нельзя переносить абсолютные числа на production.',
        '- Сохранение предпочтений и миграция видов исключены из таймера; холодный запуск proxy измерен отдельно в environment.json. Во время полного замера тесты и сборка не выполнялись.',
        '- HTTPS использует urllib: TLS-контекст кэшируется, HTTP соединение создаётся для каждого запроса. kubectl proxy управляет своими соединениями. Различие входит в результат; HTTP_PROXY в прямом режиме не используется.','',
        'CPU в исходном CSV относится ко всему Python-процессу, включая локальный TLS-стенд, и не включает дочерний kubectl. '
        'Эти значения не используются для сравнения CPU транспорта.','',
        '## Запросы одного обновления','',
        'Каждый запрос выполняется один раз, без пагинации (200 Pod помещаются на странице limit=200):','',
        '1. `GET /api/v1/namespaces` — 10 namespace.',
        '2. `GET /apis/apps/v1/deployments` — 50 Deployment.',
        '3. `GET /api/v1/pods` — 200 Pod.',
        '4. `GET /apis/apps/v1/replicasets` — 50 ReplicaSet для связи Pod → Deployment.',
        '5. `GET /apis/apps/v1/statefulsets` — пусто.',
        '6. `GET /apis/apps/v1/daemonsets` — пусто.',
        '7. `GET /api/v1/replicationcontrollers` — пусто.',
        '8. `GET /apis/batch/v1/jobs` — пусто.',
        '9. `GET /apis/batch/v1/cronjobs` — пусто.','',
        'Первым всегда читается список namespace. При 0–1 следующие списки читаются последовательно; при 2+ они планируются параллельно, '
        'поэтому порядок фактического HTTP dispatch меняется. Точное время и порядок каждой попытки записаны в requests.jsonl. '
        'Для одного snapshot имеется только восемь независимых списков после namespace: 50 настроенных потоков не создают 50 полезных задач.','',
        '## Медианы всех измеренных точек','',
        '| Потоков | Proxy: загрузка, с | Proxy: отрисовка, мс | HTTPS: загрузка, с | HTTPS: отрисовка, мс |',
        '|---:|---:|---:|---:|---:|']
    for n in range(51):
        p,h=summary['proxy'][str(n)],summary['https'][str(n)]
        lines.append(f"| {n} | {p['load_seconds']:.3f} | {p['render_ms']:.1f} | {h['load_seconds']:.3f} | {h['render_ms']:.1f} |")
    lines.extend(['','Исходные значения: [measurements.csv](measurements.csv); HTTP dispatch: [requests.jsonl](requests.jsonl); '
        'версии, условия и SHA-256 исходников: [environment.json](environment.json). Скрипты: '
        '[matrix_transports.py](../../../benchmarks/matrix_transports.py), [plot_matrix_transports.py](../../../benchmarks/plot_matrix_transports.py).',''])
    (root/'REPORT.md').write_text('\n'.join(lines),encoding='utf-8')


if __name__ == '__main__': main()
