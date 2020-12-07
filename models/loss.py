#!/usr/bin/env python3

import torch
import kornia as kn
import torch.nn as nn
import kornia.feature as kf
import torch.nn.functional as F
from utils import Visualization
from utils import PairwiseProjector
import kornia.geometry.conversions as C
from models.featurenet import GridSample


class FeatureNetLoss(nn.Module):
    def __init__(self, beta=[1, 0.5, 1], K=None, debug=False):
        super().__init__()
        self.beta = beta
        self.distinction = DistinctionLoss()
        self.projector = PairwiseProjector(K)
        self.score_loss = ScoreLoss(debug=debug)
        self.match = DiscriptorMatchLoss(debug=debug)
        self.debug = Visualization('loss') if debug else debug

    def forward(self, descriptors, points, pointness, depths_dense, poses, K, imgs):
        def batch_project(pts):
            return self.projector(pts, depths_dense, poses, K)

        H, W = pointness.size(2), pointness.size(3)
        distinction = self.distinction(descriptors)
        cornerness = self.score_loss(pointness, imgs, batch_project)
        proj_pts, invis_idx = batch_project(points)
        match = self.match(descriptors, points, proj_pts, invis_idx, H, W)

        if self.debug is not False:
            print('Loss: ', distinction, cornerness, match)
            src_idx, dst_idx, pts_idx = invis_idx
            _proj_pts = proj_pts.clone()
            _proj_pts[src_idx, dst_idx, pts_idx, :] = -2
            for dbgpts in _proj_pts:
                self.debug.show(imgs, dbgpts)
            self.debug.showmatch(imgs[0], points[0], imgs[1], proj_pts[0,1])

        return self.beta[0]*distinction + self.beta[1]*cornerness + self.beta[2]*match


class DistinctionLoss(nn.Module):
    def __init__(self):
        super().__init__()
        self.relu = nn.ReLU()
        self.cosine = PairwiseCosine(inter_batch=False)

    def forward(self, descriptors):
        pcos = self.cosine(descriptors, descriptors)
        return self.relu(pcos).mean()


class ScoreLoss(nn.Module):
    def __init__(self, radius=8, num_corners=500, debug=False):
        super(ScoreLoss, self).__init__()
        self.bceloss = nn.BCELoss()
        self.corner_det = kf.CornerGFTT()
        self.num_corners = num_corners
        self.pool = nn.MaxPool2d(kernel_size=radius, return_indices=True)
        self.unpool = nn.MaxUnpool2d(kernel_size=radius)
        self.debug = Visualization('corners') if debug else debug

    def forward(self, scores_dense, imgs, projector):
        corners = self.get_corners(imgs, projector)
        lap = kn.filters.laplacian(scores_dense, 5) # smoothness

        if self.debug:
            _B = corners.shape[0]
            _coords = corners.squeeze().nonzero(as_tuple=False)
            _pts_list = [_coords[_coords[:, 0] == i][:, [2, 1]] for i in range(_B)]
            _pts = torch.ones(_B, max([p.shape[0] for p in _pts_list]), 2) * -2
            for i, p in enumerate(_pts_list):
                _pts[i, :len(p)] = p
            _pts = C.normalize_pixel_coordinates(_pts, imgs.shape[2], imgs.shape[3])
            self.debug.show(imgs, _pts)

        return self.bceloss(scores_dense, corners) + (scores_dense * torch.exp(-lap)).mean() * 10

    def get_corners(self, imgs, projector=None):
        (B, _, H, W), N = imgs.shape, self.num_corners
        corners = kf.nms2d(self.corner_det(kn.rgb_to_grayscale(imgs)), (5, 5))

        # only one in patch
        output, indices = self.pool(corners)
        corners = self.unpool(output, indices)

        # keep top
        values, idx = corners.view(B, -1).topk(N, dim=1)
        coords = torch.stack([idx % W, idx // W], dim=2)  # (x, y), same below

        if not projector:
            # keep as-is
            b = torch.arange(0, B).repeat_interleave(N).to(idx)
            h, w = idx // W, idx % W
            values = values.flatten()
        else:
            # combine corners from all images
            coords = kn.normalize_pixel_coordinates(coords, H, W)
            coords, invis_idx = projector(coords)
            coords[tuple(invis_idx)] = -2
            coords_combined = coords.transpose(0, 1).reshape(B, B * N, 2)
            coords_combined = kn.denormalize_pixel_coordinates(coords_combined, H, W).round().to(torch.long)
            b = torch.arange(B).repeat_interleave(B * N).to(coords_combined)
            w, h = coords_combined.reshape(-1, 2).T
            mask = w >= 0
            b, h, w, values = b[mask], h[mask], w[mask], values.flatten().repeat(B)[mask]

        target = torch.zeros_like(corners)
        target[b, 0, h, w] = values
        target = kf.nms2d(target, (5, 5))

        return (target > 0).to(target)


class ScoreProjectionLoss(nn.Module):
    def __init__(self):
        super().__init__()
        self.sample = GridSample()
        self.mseloss = nn.MSELoss(reduction='none')

    def forward(self, pointness, scores_src, proj_pts, invis_idx):
        scores_dst = self.sample((pointness, proj_pts))
        scores_src = scores_src.unsqueeze(0).expand_as(scores_dst)
        proj_loss = self.mseloss(scores_dst, scores_src)
        src_idx, dst_idx, pts_idx = invis_idx
        proj_loss[src_idx, dst_idx, pts_idx] = 0
        return proj_loss.mean()


class DiscriptorMatchLoss(nn.Module):
    def __init__(self, radius=1, debug=False):
        super(DiscriptorMatchLoss, self).__init__()
        self.radius, self.debug = radius, debug
        self.cosine = PairwiseCosine(inter_batch=True)

    def forward(self, descriptors, pts_src, pts_dst, invis_idx, height, width):
        B, N, _ = pts_src.shape

        pts_src = C.denormalize_pixel_coordinates(pts_src.detach(), height, width)
        pts_dst = C.denormalize_pixel_coordinates(pts_dst.detach(), height, width)
        pts_src = pts_src.unsqueeze(0).expand_as(pts_dst).reshape(B**2, N, 2)
        pts_dst = pts_dst.reshape_as(pts_src)

        dist = torch.cdist(pts_src, pts_dst).reshape(B, B, N, N)
        dist[tuple(invis_idx)] = float('nan')
        pcos = self.cosine(descriptors, descriptors)

        match_cos = pcos[(dist <= self.radius).triu(diagonal=1)]
        _match_cos = pcos[(dist > self.radius).triu(diagonal=1)]

        return (1 - match_cos.mean()) + _match_cos.mean()


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
        xy = torch.einsum(self.eqn, x, y)
        return xy / (xx * yy).clamp(min=self.eps**2).sqrt()
