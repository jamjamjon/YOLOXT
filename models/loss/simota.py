
"""
YOLOX Loss functions
"""

import torch
import torch.nn as nn
import torch.nn.functional as F

import rich
import numpy as np
import math

from utils.torch_utils import de_parallel
from utils.metrics import bbox_iou, pairwise_bbox_iou
from utils.general import CONSOLE, LOGGER, colorstr



class ComputeLoss:
    '''
    This func contains SimOTA and siou loss.
    '''
    def __init__(self, model):
        LOGGER.info(f"{colorstr('ComputeLoss: ')} SimOTA")

        self.device = next(model.parameters()).device  # get model device
        self.hyp = model.hyp  # hyperparameters
        self.box_weight = self.hyp.get('box_weight', 5.0)
        self.iou_weight = self.hyp.get('iou_weight', 3.0)
        self.cls_weight = self.hyp.get('cls_weight', 1.0)
        self.center_radius = self.hyp.get('center_radius', 2.5)

        self.head = de_parallel(model).model[-1]  # Detect() module
        self.nl = self.head.nl
        self.na = self.head.na
        self.nc = self.head.nc
        self.stride = self.head.stride
        self.ng = 0   # number of grid in every scale: 80x80 + 40x40 + 20x20

        # Define criteria
        self.BCEcls = nn.BCEWithLogitsLoss(reduction="none")   # reduction="mean" default, pos_weights=None
        self.BCEobj = nn.BCEWithLogitsLoss(reduction="none")   # TODO: add pos_weights=None
        self.L1box = nn.L1Loss(reduction="none")



    def __call__(self, p, targets):
        # p: {(bs, 1, 80, 80, 85), ...}
        # targets: { num_object, 6(idx, cls, xywh)}

        # input size
        input_h, input_w = self.stride[0] * p[0].shape[2], self.stride[0] * p[0].shape[3] # 640, 640

        # init loss
        lcls = torch.zeros(1, device=self.device)
        lobj = torch.zeros(1, device=self.device)
        lbox = torch.zeros(1, device=self.device) 
        lbox_l1 = torch.zeros(1, device=self.device)

        # build predictions
        (   p,                          # [bs, 1, 80, 80, no] => [bs, 8400, no]
            pbox,                       # [batch, n_anchors_all, 4]
            pbox0,                      # [batch, n_anchors_all, 4]
            pobj,                       # [batch, n_anchors_all, 1]
            pcls,                       # [batch, n_anchors_all, n_cls]
            self.xy_shifts,                  # [1, n_anchors_all(8400), 2]
            self.expanded_strides,           # [1, n_anchors_all(8400), 1] 
        ) = self.build_preds(p)
        
        # build targets
        targets, nts = self.build_targets(p, targets)

        # targets cls, box, ...
        tcls, tbox, tbox_l1, tobj, finalists_masks, num_finalists = [], [], [], [], [], 0 
        
        # batch loop
        for idx in range(p.shape[0]):   # batch size
            nt = int(nts[idx])  # num of targets in current image

            if nt == 0:     # neg sample image
                tcls_ = p.new_zeros((0, self.nc))
                tbox_ = p.new_zeros((0, 4))
                tbox_l1_ = p.new_zeros((0, 4))
                tobj_ = p.new_zeros((self.ng, 1))
                finalists_mask = p.new_zeros(self.ng).bool()
            else:   
                imgsz = torch.Tensor([[input_w, input_h, input_w, input_h]]).type_as(targets)  # [[640, 640, 640, 640]]
                t_bboxes = targets[idx, :nt, 1:5].mul_(imgsz)    # gt bbox, de-scaled 
                t_classes = targets[idx, :nt, 0]   # gt cls [ 0., 40., 23., 23.]
                p_bboxes = pbox[idx]        # pred bbox
                p_classes = pcls[idx]       # pred cls
                p_objs = pobj[idx]          # pred obj

                # do label assignment: SimOTA 
                (
                    finalists_mask,
                    num_anchor_assigned,   
                    tcls_, 
                    tobj_, 
                    tbox_, 
                    tbox_l1_,
                 ) = self.get_assignments(t_bboxes, t_classes, p_bboxes, p_classes, p_objs)
                
                # num of assigned anchors in one batch
                num_finalists += num_anchor_assigned    

            # append to list
            tcls.append(tcls_)
            tbox.append(tbox_)
            tobj.append(tobj_)
            tbox_l1.append(tbox_l1_)
            finalists_masks.append(finalists_mask)

        # concat
        tcls = torch.cat(tcls, 0)
        tbox = torch.cat(tbox, 0)
        tobj = torch.cat(tobj, 0)
        tbox_l1 = torch.cat(tbox_l1, 0)
        finalists_masks = torch.cat(finalists_masks, 0)
        num_finalists = max(num_finalists, 1)

        # Compute loss
        lbox += (1.0 - bbox_iou(pbox.view(-1, 4)[finalists_masks], tbox, CIoU=True).squeeze()).sum() / num_finalists  # iou(prediction, target)
        lobj += (self.BCEobj(pobj.view(-1, 1), tobj * 1.0)).sum() / num_finalists
        lcls += (self.BCEcls(pcls.view(-1, self.nc)[finalists_masks], tcls)).sum() / num_finalists
        lbox_l1 += (self.L1box(pbox0.view(-1, 4)[finalists_masks], tbox_l1)).sum() / num_finalists
        total_loss = self.box_weight * lbox + lobj + lcls + lbox_l1

        # TODO: does L1 loss matters ?
        return total_loss, torch.cat((self.box_weight * lbox, lobj, lcls, lbox_l1)).detach()


    # build predictions
    def build_preds(self, p):
        
        xy_shifts, expanded_strides, preds_new, preds_scale = [], [], [], []

        for k, pred in enumerate(p):
            # ------------------------------------------------------------------
            # decode pred: [bs, 1, 80, 80, no] => [bs, 8400, no]
            # ------------------------------------------------------------------
            bs, _, h, w, _ = pred.shape   # [bs, na, 80, 80, no]
            grid = self.head.grid[k].to(self.device)    # [80， 40， 20] in 640

            # grid init at the 1st time
            if grid.shape[2:4] != pred.shape[2:4]:
                yv, xv = torch.meshgrid([torch.arange(h), torch.arange(w)])
                grid = torch.stack((xv, yv), 2).view(1, 1, h, w, 2).to(self.device)
                self.head.grid[k] = grid    # [1, 1, 80, 80, 2]

            pred = pred.reshape(bs, self.na * h * w, -1)    # （bs, 80x80, 85）
            pred_scale = pred.clone()   # clone

            # de-scale to img size
            xy_shift = grid.view(1, -1, 2)  # [1, 6400, 2])
            pred[..., :2] = (pred[..., :2] + xy_shift) * self.stride[k]     # xy
            pred[..., 2:4] = torch.exp(pred[..., 2:4]) * self.stride[k]     # wh
            # ------------------------------------------------------------------

            # stride between grid 
            expanded_stride = torch.full((1, xy_shift.shape[1], 1), self.stride[k], device=self.device)     #[1, 6400, 1]

            # append to list
            xy_shifts.append(xy_shift)
            expanded_strides.append(expanded_stride)
            preds_new.append(pred)              # [[16, 6400, 85], [16, 1600, 85], [16, 400, 85]]
            preds_scale.append(pred_scale)      # [[16, 6400, 85], [16, 1600, 85], [16, 400, 85]]

        # concat
        xy_shifts = torch.cat(xy_shifts, 1)                 # [1, n_anchors_all(8400), 2]
        expanded_strides = torch.cat(expanded_strides, 1)   # [1, n_anchors_all(8400), 1]
        preds_scale = torch.cat(preds_scale, 1)             # [16, 8400, 85]
        p = torch.cat(preds_new, 1)                     # [16, 8400, 85]

        pbox = p[:, :, :4]                  # at input size. [batch, n_anchors_all, 4]
        pbox0 = preds_scale[:, :, :4]       # at scales, for l1 loss compute. [batch, n_anchors_all, 4]
        pobj = p[:, :, 4].unsqueeze(-1)     # [batch, n_anchors_all, 1]
        pcls = p[:, :, 5:]                  # [batch, n_anchors_all, n_cls]

        self.ng = p.shape[1]      # 80x80 + 40x40 + 20x20

        return p, pbox, pbox0, pobj, pcls, xy_shifts, expanded_strides


    # build targets
    def build_targets(self, p, targets):
        targets_list = np.zeros((p.shape[0], 1, 5)).tolist()   # batch size
        for i, item in enumerate(targets.cpu().numpy().tolist()):
            targets_list[int(item[0])].append(item[1:])
        max_len = max((len(l) for l in targets_list))
        targets = torch.from_numpy(np.array(list(map(lambda l:l + [[-1,0,0,0,0]]*(max_len - len(l)), targets_list)))[:,1:,:]).to(self.device)
        nts = (targets.sum(dim=2) > 0).sum(dim=1)  # number of objects list per batch [13, 4, 2, ...]
        return targets, nts



    # SimOTA
    @torch.no_grad()
    def get_assignments(self, t_bboxes, t_classes, p_bboxes, p_classes, p_objs):

        num_objects = t_bboxes.shape[0]   # number of gt object per image

        # 1. get candidates: {a fixed center region} + {gt box} 
        candidates_mask, is_in_boxes_and_center = self.get_in_boxes_info(t_bboxes)

        # 2. pick preds in fixed center region, and get bbox, cls, obj
        p_bboxes = p_bboxes[candidates_mask]
        cls_preds_ = p_classes[candidates_mask]
        obj_preds_ = p_objs[candidates_mask]
        num_in_boxes_anchor = p_bboxes.shape[0]

        # 3. iou loss => iou(gts, preds), for calculate dynamic_k
        pair_wise_ious = pairwise_bbox_iou(t_bboxes, p_bboxes, box_format='xywh')
        pair_wise_ious_loss = -torch.log(pair_wise_ious + 1e-8)
        
        # 4. cls loss = cls * obj
        gt_cls_per_image = (F.one_hot(t_classes.to(torch.int64), self.nc)
                            .float()
                            .unsqueeze(1)
                            .repeat(1, num_in_boxes_anchor, 1))   # gt classes to one hot
        
        with torch.cuda.amp.autocast(enabled=False):
            cls_preds_ = (cls_preds_.float().sigmoid_().unsqueeze(0).repeat(num_objects, 1, 1) 
                          * obj_preds_.float().sigmoid_().unsqueeze(0).repeat(num_objects, 1, 1))

            pair_wise_cls_loss = F.binary_cross_entropy(cls_preds_.sqrt_(), gt_cls_per_image, reduction="none").sum(-1)
        del cls_preds_, obj_preds_

        # 5. cost
        cost = (self.cls_weight * pair_wise_cls_loss        # 1.0
                + self.iou_weight * pair_wise_ious_loss     # 3.0
                + 100000.0 * (~is_in_boxes_and_center))     # neg samples

        # 6. assign different k positive samples for every gt. 给每个gt分配k个正样本 
        (   
            num_anchor_assigned,
            pred_ious_this_matching,
            matched_gt_inds,
            finalists_mask     
        ) = self.dynamic_k_matching(cost, pair_wise_ious, t_classes, candidates_mask)

        del pair_wise_cls_loss, cost, pair_wise_ious, pair_wise_ious_loss

        # 7. empty cuda cache
        torch.cuda.empty_cache() 

        # 8. has anchor point assigned
        if num_anchor_assigned > 0:
            # tcls, tbox, tobj
            tcls_ = t_classes[matched_gt_inds]
            tcls_ = F.one_hot(tcls_.to(torch.int64), self.nc) * pred_ious_this_matching.unsqueeze(-1)
            tobj_ = finalists_mask.unsqueeze(-1)
            tbox_ = t_bboxes[matched_gt_inds]

            # tbox_l1, do scale
            tbox_l1_ = p_bboxes.new_zeros((num_anchor_assigned, 4))
            stride_ = self.expanded_strides[0][finalists_mask]
            grid_ = self.xy_shifts[0][finalists_mask]
            tbox_l1_[:, :2] = t_bboxes[matched_gt_inds][:, :2] / stride_ - grid_
            tbox_l1_[:, 2:4] = torch.log(t_bboxes[matched_gt_inds][:, 2:4] / stride_ + 1e-8)

        return finalists_mask, num_anchor_assigned, tcls_, tobj_, tbox_, tbox_l1_


    # get candidates: a fixed center region
    def get_in_boxes_info(self, t_bboxes):

        num_object = t_bboxes.shape[0]  # number of objects

        expanded_strides_per_image = self.expanded_strides[0]
        xy_shifts_per_image = self.xy_shifts[0] * expanded_strides_per_image
        xy_centers_per_image = (
            (xy_shifts_per_image + 0.5 * expanded_strides_per_image)
            .unsqueeze(0)
            .repeat(num_object, 1, 1)
        )  # [n_anchor, 2] -> [n_gt, n_anchor, 2]

        gt_bboxes_per_image_lt = (
            (t_bboxes[:, 0:2] - 0.5 * t_bboxes[:, 2:4])
            .unsqueeze(1)
            .repeat(1, self.ng, 1)
        )
        gt_bboxes_per_image_rb = (
            (t_bboxes[:, 0:2] + 0.5 * t_bboxes[:, 2:4])
            .unsqueeze(1)
            .repeat(1, self.ng, 1)
        )  # [n_gt, 2] -> [n_gt, n_anchor, 2]

        b_lt = xy_centers_per_image - gt_bboxes_per_image_lt
        b_rb = gt_bboxes_per_image_rb - xy_centers_per_image
        bbox_deltas = torch.cat([b_lt, b_rb], 2)

        is_in_boxes = bbox_deltas.min(dim=-1).values > 0.0
        is_in_boxes_all = is_in_boxes.sum(dim=0) > 0

        # in fixed center
        gt_bboxes_per_image_lt = (t_bboxes[:, 0:2]).unsqueeze(1).repeat(
            1, self.ng, 1
        ) - self.center_radius * expanded_strides_per_image.unsqueeze(0)
        gt_bboxes_per_image_rb = (t_bboxes[:, 0:2]).unsqueeze(1).repeat(
            1, self.ng, 1
        ) + self.center_radius * expanded_strides_per_image.unsqueeze(0)

        c_lt = xy_centers_per_image - gt_bboxes_per_image_lt
        c_rb = gt_bboxes_per_image_rb - xy_centers_per_image
        center_deltas = torch.cat([c_lt, c_rb], 2)
        is_in_centers = center_deltas.min(dim=-1).values > 0.0
        is_in_centers_all = is_in_centers.sum(dim=0) > 0

        # in gt boxes or in centers region
        is_in_boxes_anchor = is_in_boxes_all | is_in_centers_all

        # in gt boxes and in centers region
        is_in_boxes_and_center = (
            is_in_boxes[:, is_in_boxes_anchor] & is_in_centers[:, is_in_boxes_anchor]
        )

        return is_in_boxes_anchor, is_in_boxes_and_center


    # assign different k positive samples for every gt. 给每个gt分配k个正样本 
    def dynamic_k_matching(self, cost, pair_wise_ious, t_classes, candidates_mask):
        
        ious_in_boxes_matrix = pair_wise_ious   # iou matrix 

        # 1 给当前gt匹配10个iou最大的anchor point
        n_candidate_k = min(10, ious_in_boxes_matrix.size(1))
        topk_ious, _ = torch.topk(ious_in_boxes_matrix, n_candidate_k, dim=1)
        
        # 2 将10个anchor point的iou求和并向下取整，得到dynamkic-k
        dynamic_ks = torch.clamp(topk_ious.sum(1).int(), min=1)
        dynamic_ks = dynamic_ks.tolist()

        # 最后gt和anchor point匹配到的矩阵，gt分配的anchor为1，其余为0
        matching_matrix = torch.zeros_like(cost, dtype=torch.uint8)
        
        # 3 根据cost来为每个gt分配k个anchor
        for gt_idx in range(t_classes.shape[0]):   # number of objects
            _, pos_idx = torch.topk(cost[gt_idx], k=dynamic_ks[gt_idx], largest=False)
            matching_matrix[gt_idx][pos_idx] = 1
        del topk_ious, dynamic_ks, pos_idx

        # 4 过滤到共用的anchor point
        anchor_matching_gt = matching_matrix.sum(0)
        if (anchor_matching_gt > 1).sum() > 0:
            _, cost_argmin = torch.min(cost[:, anchor_matching_gt > 1], dim=0)
            matching_matrix[:, anchor_matching_gt > 1] *= 0
            matching_matrix[cost_argmin, anchor_matching_gt > 1] = 1

        # 5 所有gt一共分配到了多少anchor
        fg_mask_inboxes = matching_matrix.sum(0) > 0
        num_anchor_assigned = fg_mask_inboxes.sum().item()

        # 6. update candidates_mask
        candidates_mask[candidates_mask.clone()] = fg_mask_inboxes
        matched_gt_inds = matching_matrix[:, fg_mask_inboxes].argmax(0)

        # 7 matching_matrix(其中除了0就是1) * iou matrix
        pred_ious_this_matching = (matching_matrix * pair_wise_ious).sum(0)[
            fg_mask_inboxes
        ]

        # 8 finalists_mask
        finalists_mask = candidates_mask

        return num_anchor_assigned, pred_ious_this_matching, matched_gt_inds, finalists_mask