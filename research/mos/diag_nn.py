"""Why does the 22-station MLP peak after one epoch? Fold-0 training curves under a few settings (research).

Logs held-out-month validation loss every 200 updates for 4 epochs, then scores the best
checkpoint on fold 0 (all stations and KCDW). Settings: the current one (lr 1e-3, weight decay
1e-4, dropout 0.1), a lower learning rate, lower learning rate with strong regularization,
and the current setting without the station-history inputs.
"""
import time

import numpy as np
import torch
from torch import nn

import multi_train as mt
import nn_arch as na
import nn_multi as nm
from compare_common import LEVELS, crps
from compare_nn import TRAIN_LEVELS, bernstein

CONFIGS = [('current: lr 1e-3, wd 1e-4, dropout 0.1', 1e-3, 1e-4, 0.1, True), ('lr 3e-4', 3e-4, 1e-4, 0.1, True),
           ('lr 3e-4, wd 1e-2, dropout 0.3', 3e-4, 1e-2, 0.3, True), ('current, without station history', 1e-3, 1e-4, 0.1, False)]


def main():
    t = mt.load()
    train = (t.fold != 0).to_numpy()
    mt.add_history(t, train)
    months = (t.date.dt.year * 12 + t.date.dt.month).to_numpy()
    fit, val, test = train & (months % 7 != 3), train & (months % 7 == 3), ~train
    y = torch.tensor(t.gust_peak.to_numpy('float32'), device='cuda')
    stations = sorted(t.station.unique())
    s_all = torch.tensor(t.station.map({s: i for i, s in enumerate(stations)}).to_numpy('int64'), device='cuda')
    cdw = (t.station == 'CDW').to_numpy()[test]
    basis, lv = bernstein(TRAIN_LEVELS).cuda(), TRAIN_LEVELS.cuda()
    for name, lr, wd, drop, history in CONFIGS:
        cols = [c for c in mt.features(t, 'site') if history or not c.startswith('hist_')]
        X, _ = nm.design(t, cols, train)
        Xt = torch.tensor(X, device='cuda'); del X
        torch.manual_seed(0)
        net = nm.SiteMLP(Xt.shape[1], len(stations)).cuda()
        for m in net.body:
            if isinstance(m, nn.Dropout):
                m.p = drop
        opt = torch.optim.AdamW(net.parameters(), lr=lr, weight_decay=wd)
        fit_i, val_i = torch.tensor(np.flatnonzero(fit), device='cuda'), torch.tensor(np.flatnonzero(val), device='cuda')

        def vloss():
            net.eval()
            with torch.no_grad():
                out = [na.pinball(net(Xt[i], None, s_all[i]), y[i], basis, lv) for i in val_i.split(16384)]
            net.train()
            return torch.cat(out).mean().item()

        best, best_state, curve, step, started = 9e9, None, [], 0, time.time()
        for epoch in range(4):
            perm = fit_i[torch.randperm(len(fit_i), device='cuda')]
            for i in range(0, len(perm), 4096):
                idx = perm[i:i + 4096]
                opt.zero_grad()
                loss = na.pinball(net(Xt[idx], None, s_all[idx]), y[idx], basis, lv).mean()
                loss.backward(); opt.step(); step += 1
                if step % 200 == 0:
                    v = vloss(); curve.append((step, round(v, 4)))
                    if v < best:
                        best, best_state = v, {k: x.detach().clone() for k, x in net.state_dict().items()}
        net.load_state_dict(best_state); net.eval()
        test_i = torch.tensor(np.flatnonzero(test), device='cuda')
        q = na.predict(net, (Xt.cpu().numpy(), np.zeros((len(t), 1), 'uint8'), s_all.cpu().numpy()), np.flatnonzero(test))
        q = np.sort(np.maximum(q, 0), axis=1)
        c = crps(t.gust_peak.to_numpy()[test], q)
        per_epoch = len(fit_i) // 4096
        print(f'{name}: {per_epoch} updates/epoch; val every 200 updates: {curve}', flush=True)
        print(f'  best val {best:.4f} at update {min(curve, key=lambda r: r[1])[0]}; fold-0 CRPS all {c.mean():.4f}, KCDW {c[cdw].mean():.4f} ({time.time() - started:.0f} s)', flush=True)
        del net, Xt; torch.cuda.empty_cache()


if __name__ == '__main__':
    main()
