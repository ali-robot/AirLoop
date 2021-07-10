import torch
import torch.nn as nn
import torch.nn.functional as F


class GridSample(nn.Module):
    def __init__(self, mode='bilinear'):
        super().__init__()
        self.mode = mode

    def forward(self, inputs):
        features, points = inputs
        dim = len(points.shape)
        points = points.view(features.size(0), 1, -1, 2) if dim == 3 else points
        output = F.grid_sample(features, points, self.mode, align_corners=True).permute(0, 2, 3, 1)
        return output.squeeze(1) if dim == 3 else output


class PairwiseCosine(nn.Module):
    def __init__(self, inter_batch=False, dim=-1, eps=1e-8):
        super(PairwiseCosine, self).__init__()
        self.inter_batch, self.dim, self.eps = inter_batch, dim, eps
        self.eqn = 'amd,bnd->abmn' if inter_batch else 'bmd,bnd->bmn'

    def forward(self, x, y):
        xx = torch.sum(x**2, dim=self.dim).unsqueeze(-1) # (A, M, 1)
        yy = torch.sum(y**2, dim=self.dim).unsqueeze(-2) # (B, 1, N)
        if self.inter_batch:
            xx, yy = xx.unsqueeze(1), yy.unsqueeze(0) # (A, 1, M, 1), (1, B, 1, N)
        xy = torch.einsum(self.eqn, x, y) if x.shape[1] > 0 else torch.zeros_like(xx * yy)
        return xy / (xx * yy).clamp(min=self.eps**2).sqrt()


class ConsecutiveMatch(nn.Module):
    def __init__(self, metric=PairwiseCosine()):
        super().__init__()
        self.metric = metric

    def forward(self, desc_src, desc_dst, points_dst):
        confidence, idx = self.metric(desc_src, desc_dst).max(dim=2)
        matched = points_dst.gather(1, idx.unsqueeze(2).expand(-1, -1, 2))

        return matched, confidence