from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import logging
import math
from os.path import join

import numpy as np
import torch
import torch.nn.functional as F
import torch.utils.model_zoo as model_zoo
from torch import nn
from ..decode import _topk, _max_pool, _gather_feat


from dcn_v2 import DCN

# from lib.models.networks.DCNv2.dcn_v2 import DCN

BN_MOMENTUM = 0.1
logger = logging.getLogger(__name__)


def get_model_url(data='imagenet', name='dla34', hash='ba72cf86'):
    return join('http://dl.yf.io/dla/models', data, '{}-{}.pth'.format(name, hash))


def conv3x3(in_planes, out_planes, stride=1):
    "3x3 convolution with padding"
    return nn.Conv2d(in_planes, out_planes, kernel_size=3, stride=stride,
                     padding=1, bias=False)


class BasicBlock(nn.Module):
    def __init__(self, inplanes, planes, stride=1, dilation=1):
        super(BasicBlock, self).__init__()
        self.conv1 = nn.Conv2d(inplanes, planes, kernel_size=3,
                               stride=stride, padding=dilation,
                               bias=False, dilation=dilation)
        self.bn1 = nn.BatchNorm2d(planes, momentum=BN_MOMENTUM)
        self.relu = nn.ReLU(inplace=True)
        self.conv2 = nn.Conv2d(planes, planes, kernel_size=3,
                               stride=1, padding=dilation,
                               bias=False, dilation=dilation)
        self.bn2 = nn.BatchNorm2d(planes, momentum=BN_MOMENTUM)
        self.stride = stride

    def forward(self, x, residual=None):
        if residual is None:
            residual = x

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.bn2(out)

        out += residual
        out = self.relu(out)

        return out


class Bottleneck(nn.Module):
    expansion = 2

    def __init__(self, inplanes, planes, stride=1, dilation=1):
        super(Bottleneck, self).__init__()
        expansion = Bottleneck.expansion
        bottle_planes = planes // expansion
        self.conv1 = nn.Conv2d(inplanes, bottle_planes,
                               kernel_size=1, bias=False)
        self.bn1 = nn.BatchNorm2d(bottle_planes, momentum=BN_MOMENTUM)
        self.conv2 = nn.Conv2d(bottle_planes, bottle_planes, kernel_size=3,
                               stride=stride, padding=dilation,
                               bias=False, dilation=dilation)
        self.bn2 = nn.BatchNorm2d(bottle_planes, momentum=BN_MOMENTUM)
        self.conv3 = nn.Conv2d(bottle_planes, planes,
                               kernel_size=1, bias=False)
        self.bn3 = nn.BatchNorm2d(planes, momentum=BN_MOMENTUM)
        self.relu = nn.ReLU(inplace=True)
        self.stride = stride

    def forward(self, x, residual=None):
        if residual is None:
            residual = x

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.bn2(out)
        out = self.relu(out)

        out = self.conv3(out)
        out = self.bn3(out)

        out += residual
        out = self.relu(out)

        return out


class BottleneckX(nn.Module):
    expansion = 2
    cardinality = 32

    def __init__(self, inplanes, planes, stride=1, dilation=1):
        super(BottleneckX, self).__init__()
        cardinality = BottleneckX.cardinality
        # dim = int(math.floor(planes * (BottleneckV5.expansion / 64.0)))
        # bottle_planes = dim * cardinality
        bottle_planes = planes * cardinality // 32
        self.conv1 = nn.Conv2d(inplanes, bottle_planes,
                               kernel_size=1, bias=False)
        self.bn1 = nn.BatchNorm2d(bottle_planes, momentum=BN_MOMENTUM)
        self.conv2 = nn.Conv2d(bottle_planes, bottle_planes, kernel_size=3,
                               stride=stride, padding=dilation, bias=False,
                               dilation=dilation, groups=cardinality)
        self.bn2 = nn.BatchNorm2d(bottle_planes, momentum=BN_MOMENTUM)
        self.conv3 = nn.Conv2d(bottle_planes, planes,
                               kernel_size=1, bias=False)
        self.bn3 = nn.BatchNorm2d(planes, momentum=BN_MOMENTUM)
        self.relu = nn.ReLU(inplace=True)
        self.stride = stride

    def forward(self, x, residual=None):
        if residual is None:
            residual = x

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.bn2(out)
        out = self.relu(out)

        out = self.conv3(out)
        out = self.bn3(out)

        out += residual
        out = self.relu(out)

        return out


class Root(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size, residual):
        super(Root, self).__init__()
        self.conv = nn.Conv2d(
            in_channels, out_channels, 1,
            stride=1, bias=False, padding=(kernel_size - 1) // 2)
        self.bn = nn.BatchNorm2d(out_channels, momentum=BN_MOMENTUM)
        self.relu = nn.ReLU(inplace=True)
        self.residual = residual

    def forward(self, *x):
        children = x
        x = self.conv(torch.cat(x, 1))
        x = self.bn(x)
        if self.residual:
            x += children[0]
        x = self.relu(x)

        return x


class Tree(nn.Module):
    def __init__(self, levels, block, in_channels, out_channels, stride=1,
                 level_root=False, root_dim=0, root_kernel_size=1,
                 dilation=1, root_residual=False):
        super(Tree, self).__init__()
        if root_dim == 0:
            root_dim = 2 * out_channels
        if level_root:
            root_dim += in_channels
        if levels == 1:
            self.tree1 = block(in_channels, out_channels, stride,
                               dilation=dilation)
            self.tree2 = block(out_channels, out_channels, 1,
                               dilation=dilation)
        else:
            self.tree1 = Tree(levels - 1, block, in_channels, out_channels,
                              stride, root_dim=0,
                              root_kernel_size=root_kernel_size,
                              dilation=dilation, root_residual=root_residual)
            self.tree2 = Tree(levels - 1, block, out_channels, out_channels,
                              root_dim=root_dim + out_channels,
                              root_kernel_size=root_kernel_size,
                              dilation=dilation, root_residual=root_residual)
        if levels == 1:
            self.root = Root(root_dim, out_channels, root_kernel_size,
                             root_residual)
        self.level_root = level_root
        self.root_dim = root_dim
        self.downsample = None
        self.project = None
        self.levels = levels
        if stride > 1:
            self.downsample = nn.MaxPool2d(stride, stride=stride)
        if in_channels != out_channels:
            self.project = nn.Sequential(
                nn.Conv2d(in_channels, out_channels,
                          kernel_size=1, stride=1, bias=False),
                nn.BatchNorm2d(out_channels, momentum=BN_MOMENTUM)
            )

    def forward(self, x, residual=None, children=None):
        children = [] if children is None else children
        bottom = self.downsample(x) if self.downsample else x
        residual = self.project(bottom) if self.project else bottom
        if self.level_root:
            children.append(bottom)
        x1 = self.tree1(x, residual)
        if self.levels == 1:
            x2 = self.tree2(x1)
            x = self.root(x2, x1, *children)
        else:
            children.append(x1)
            x = self.tree2(x1, children=children)
        return x


class DLA(nn.Module):
    def __init__(self, levels, channels, num_classes=1000,
                 block=BasicBlock, residual_root=False, linear_root=False):
        super(DLA, self).__init__()
        self.channels = channels
        self.num_classes = num_classes
        self.base_layer = nn.Sequential(
            nn.Conv2d(3, channels[0], kernel_size=7, stride=1,
                      padding=3, bias=False),
            nn.BatchNorm2d(channels[0], momentum=BN_MOMENTUM),
            nn.ReLU(inplace=True))
        self.level0 = self._make_conv_level(
            channels[0], channels[0], levels[0])
        self.level1 = self._make_conv_level(
            channels[0], channels[1], levels[1], stride=2)
        self.level2 = Tree(levels[2], block, channels[1], channels[2], 2,
                           level_root=False,
                           root_residual=residual_root)
        self.level3 = Tree(levels[3], block, channels[2], channels[3], 2,
                           level_root=True, root_residual=residual_root)
        self.level4 = Tree(levels[4], block, channels[3], channels[4], 2,
                           level_root=True, root_residual=residual_root)
        self.level5 = Tree(levels[5], block, channels[4], channels[5], 2,
                           level_root=True, root_residual=residual_root)

        # for m in self.modules():
        #     if isinstance(m, nn.Conv2d):
        #         n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
        #         m.weight.data.normal_(0, math.sqrt(2. / n))
        #     elif isinstance(m, nn.BatchNorm2d):
        #         m.weight.data.fill_(1)
        #         m.bias.data.zero_()

    def _make_level(self, block, inplanes, planes, blocks, stride=1):
        downsample = None
        if stride != 1 or inplanes != planes:
            downsample = nn.Sequential(
                nn.MaxPool2d(stride, stride=stride),
                nn.Conv2d(inplanes, planes,
                          kernel_size=1, stride=1, bias=False),
                nn.BatchNorm2d(planes, momentum=BN_MOMENTUM),
            )

        layers = []
        layers.append(block(inplanes, planes, stride, downsample=downsample))
        for i in range(1, blocks):
            layers.append(block(inplanes, planes))

        return nn.Sequential(*layers)

    def _make_conv_level(self, inplanes, planes, convs, stride=1, dilation=1):
        modules = []
        for i in range(convs):
            modules.extend([
                nn.Conv2d(inplanes, planes, kernel_size=3,
                          stride=stride if i == 0 else 1,
                          padding=dilation, bias=False, dilation=dilation),
                nn.BatchNorm2d(planes, momentum=BN_MOMENTUM),
                nn.ReLU(inplace=True)])
            inplanes = planes
        return nn.Sequential(*modules)

    def forward(self, x):
        y = []
        x = self.base_layer(x)
        for i in range(6):
            x = getattr(self, 'level{}'.format(i))(x)
            y.append(x)
        return y

    def load_pretrained_model(self, data='imagenet', name='dla34', hash='ba72cf86'):
        # fc = self.fc
        if name.endswith('.pth'):
            model_weights = torch.load(data + name)
        else:
            model_url = get_model_url(data, name, hash)
            model_weights = model_zoo.load_url(model_url)
        num_classes = len(model_weights[list(model_weights.keys())[-1]])
        self.fc = nn.Conv2d(
            self.channels[-1], num_classes,
            kernel_size=1, stride=1, padding=0, bias=True)
        self.load_state_dict(model_weights)
        # self.fc = fc


def dla34(pretrained=True, **kwargs):  # DLA-34
    model = DLA([1, 1, 1, 2, 2, 1],
                [16, 32, 64, 128, 256, 512],
                block=BasicBlock, **kwargs)
    if pretrained:
        model.load_pretrained_model(data='imagenet', name='dla34', hash='ba72cf86')

    return model


class Identity(nn.Module):

    def __init__(self):
        super(Identity, self).__init__()

    def forward(self, x):
        return x


def fill_fc_weights(layers):
    for m in layers.modules():
        if isinstance(m, nn.Conv2d):
            if m.bias is not None:
                nn.init.constant_(m.bias, 0)


def fill_up_weights(up):
    w = up.weight.data
    f = math.ceil(w.size(2) / 2)
    c = (2 * f - 1 - f % 2) / (2. * f)
    for i in range(w.size(2)):
        for j in range(w.size(3)):
            w[0, 0, i, j] = \
                (1 - math.fabs(i / f - c)) * (1 - math.fabs(j / f - c))
    for c in range(1, w.size(0)):
        w[c, 0, :, :] = w[0, 0, :, :]


class DeformConv(nn.Module):
    def __init__(self, chi, cho):
        super(DeformConv, self).__init__()
        self.actf = nn.Sequential(
            nn.BatchNorm2d(cho, momentum=BN_MOMENTUM),
            nn.ReLU(inplace=True)
        )
        self.conv = DCN(chi, cho, kernel_size=(3, 3), stride=1, padding=1, dilation=1, deformable_groups=1)

    def forward(self, x):
        x = self.conv(x)
        x = self.actf(x)
        return x


class IDAUp(nn.Module):
    def __init__(self, o, channels, up_f):
        super(IDAUp, self).__init__()
        for i in range(1, len(channels)):
            c = channels[i]
            f = int(up_f[i])
            proj = DeformConv(c, o)
            node = DeformConv(o, o)

            up = nn.ConvTranspose2d(o, o, f * 2, stride=f,
                                    padding=f // 2, output_padding=0,
                                    groups=o, bias=False)
            fill_up_weights(up)

            setattr(self, 'proj_' + str(i), proj)
            setattr(self, 'up_' + str(i), up)
            setattr(self, 'node_' + str(i), node)

    def forward(self, layers, startp, endp):
        for i in range(startp + 1, endp):
            upsample = getattr(self, 'up_' + str(i - startp))
            project = getattr(self, 'proj_' + str(i - startp))
            layers[i] = upsample(project(layers[i]))
            node = getattr(self, 'node_' + str(i - startp))
            layers[i] = node(layers[i] + layers[i - 1])


class DLAUp(nn.Module):
    def __init__(self, startp, channels, scales, in_channels=None):
        """
        :param startp:
        :param channels:
        :param scales:
        :param in_channels:
        """
        super(DLAUp, self).__init__()

        self.start_p = startp

        if in_channels is None:
            in_channels = channels

        self.channels = channels
        channels = list(channels)
        scales = np.array(scales, dtype=int)

        for i in range(len(channels) - 1):
            j = -i - 2
            setattr(self, 'ida_{}'.format(i),
                    IDAUp(channels[j], in_channels[j:],
                          scales[j:] // scales[j]))
            scales[j + 1:] = scales[j]
            in_channels[j + 1:] = [channels[j] for _ in channels[j + 1:]]

    def forward(self, layers):
        out = [layers[-1]]  # start with 32
        for i in range(len(layers) - self.start_p - 1):
            ida = getattr(self, 'ida_{}'.format(i))
            ida(layers, len(layers) - i - 2, len(layers))
            out.insert(0, layers[-1])

        return out


class Interpolate(nn.Module):
    def __init__(self, scale, mode):
        super(Interpolate, self).__init__()
        self.scale = scale
        self.mode = mode

    def forward(self, x):
        x = F.interpolate(x, scale_factor=self.scale, mode=self.mode, align_corners=False)
        return x


def Exteact_ID_feature(pre_output, K=128):
    heatmap = pre_output['hm'].detach()
    heatmap = _max_pool(torch.sigmoid(heatmap))
    batch, C, H, W = heatmap.size()


    topk_scores, topk_inds = torch.topk(heatmap.view(batch, C, -1), K)
    topk_score, topk_ind = torch.topk(topk_scores.view(batch, -1), K)

    topk_inds = _gather_feat(topk_inds.view(batch, -1, 1), topk_ind).view(batch, K)
    id_feature = pre_output['id'].detach().reshape(batch, 128, H * W)
    pre_id_feature = []

    for i in range(batch):
        feature = torch.index_select(id_feature[i, :, :], 1, topk_inds[i, :])
        pre_id_feature.append(feature.unsqueeze(0))
    ID_top = torch.cat(pre_id_feature, 0)
    return ID_top


class MAX_CSAM(nn.Module):

    def __init__(self, k_size=3, ch=128, s_state=False, c_state=False):
        super(MAX_CSAM, self).__init__()
        self.avg_pool = nn.AdaptiveAvgPool2d(1)
        self.max_pool = nn.AdaptiveMaxPool2d(1)
        self.sigmoid = nn.Sigmoid()

        self.s_state = s_state
        self.c_state = c_state

        if c_state:
            self.c_attention = nn.Sequential(nn.Conv1d(1, 1, kernel_size=k_size, padding=(k_size - 1) // 2, bias=False),
                                                  nn.LayerNorm([1, ch]),
                                                  nn.LeakyReLU(0.3, inplace=True),
                                                  nn.Linear(ch, ch, bias=False))

        if s_state:
            self.s_attention = nn.Conv2d(1, 1, 7, padding=3, bias=False)

    def forward(self, x):
        # x: input features with shape [b, c, h, w]
        b, c, h, w = x.size()

        # channel_attention
        if self.c_state:
            # y_avg = self.avg_pool(x)
            y_max = self.max_pool(x)
            # y_c = self.c_attention(y_avg.squeeze(-1).transpose(-1, -2)).transpose(-1, -2).unsqueeze(-1)+\
            #       self.c_attention(y_max.squeeze(-1).transpose(-1, -2)).transpose(-1, -2).unsqueeze(-1)
            y_c = self.c_attention(y_max.squeeze(-1).transpose(-1, -2)).transpose(-1, -2).unsqueeze(-1)

            y_c = self.sigmoid(y_c)

        #spatial_attention
        if self.s_state:
            # avg_out = torch.mean(x, dim=1, keepdim=True)
            max_out, _ = torch.max(x, dim=1, keepdim=True)
            # y_s = torch.cat([avg_out, max_out], dim=1)
            y_s = self.sigmoid(self.s_attention(max_out))

        if self.c_state and self.s_state:
            y = x * y_s * y_c + x
        elif self.c_state:
            y = x * y_c + x
        elif self.s_state:
            y = x * y_s + x
        else:
            y = x
        return y


class TDRM(nn.Module):
    def __init__(self):
        super(TDRM, self).__init__()
        self.in_channels = 96

        self.SAAN = MAX_CSAM(c_state=True, s_state=True)
        self.conv = nn.Conv2d(128, 32, kernel_size=1, stride=1, padding=0, bias=False)

        self.c_attention = nn.Sequential(nn.Conv2d(self.in_channels, self.in_channels, kernel_size=3, stride=1, padding=1, bias=True),
                                         nn.InstanceNorm2d(num_features=self.in_channels),
                                         nn.LeakyReLU(0.3, inplace=True),
                                         nn.Conv2d(self.in_channels,  64, kernel_size=1, stride=1, padding=1//2, bias=True))

    def forward(self, pre_k, id_map, y_1):
        batch, dim, top_k = pre_k.shape
        batch, dim, ht, wd = id_map.shape

        pre_cat = pre_k
        pre_cat = pre_cat.contiguous().view(batch, top_k, dim)
        corr = torch.matmul(pre_cat, id_map.contiguous().view(batch, dim, ht * wd))
        corr = corr.view(batch, -1, ht, wd)  # 4, k, h, w
        # xx = corr[0].detach().cpu().numpy()
        # 通道注意力
        corr_attention = self.SAAN(corr)
        # print(corr_attention.requires_grad())
        corr_attention = self.conv(corr_attention)
        # print(corr_attention.requires_grad)
        M = torch.cat((corr_attention, y_1), dim=1)
        # print(M.requires_grad)
        M = self.c_attention(M)
        # xx = M.detach().cpu().numpy()
        # xx = 1
        return M


class TEBM(nn.Module):
    def __init__(self, ch):
        super(TEBM, self).__init__()

        self.in_channels = ch
        self.c_attention = nn.Sequential(nn.Conv2d(self.in_channels, self.in_channels, kernel_size=1, stride=1, padding=0, bias=True),
                                         nn.InstanceNorm2d(num_features=self.in_channels),
                                         nn.LeakyReLU(0.3, inplace=True),
                                         nn.Conv2d(self.in_channels,  self.in_channels, kernel_size=1, stride=1, padding=1//2, bias=True))

        self.W = nn.Sequential(
            nn.Conv2d(self.in_channels, self.in_channels,
                    kernel_size=1, stride=1, padding=0, bias=True),
            nn.BatchNorm2d(self.in_channels)
        )

        self.avg_pool = nn.AdaptiveAvgPool2d(1)
        self.pool = nn.MaxPool2d(kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))

        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
                m.weight.data.normal_(0, math.sqrt(2. / n))
            elif isinstance(m, nn.BatchNorm2d):
                m.weight.data.fill_(1)
                m.bias.data.zero_()

        nn.init.constant_(self.W[1].weight.data, 0.0)
        nn.init.constant_(self.W[1].bias.data, 0.0)

    def forward(self, pre_id, id):
        b, c, h, w = pre_id.size()

        inputs = id
        query_pool = self.avg_pool(id).squeeze(-1).permute(0, 2, 1)  # b,1,c

        memory = self.pool(pre_id)
        memory = memory.contiguous().view(b, c, -1)
        # b, c, hw
        f = torch.matmul(query_pool, memory)  # b,1,c  b,c,hw -> b,1,hw

        y = torch.matmul(f, memory.permute(0, 2, 1))
        y = y.permute(0, 2, 1).unsqueeze(-1)
        # f = f.permute(0, 2, 1).view(b, h, w, -1).squeeze(-1)
        y = self.W(y)

        inputs = inputs * y + inputs
        # x = y.detach().cpu().numpy()
        # xx =1
        inputs = self.c_attention(inputs)
        return inputs


class DLASeg(nn.Module):
    def __init__(self,
                 base_name,
                 heads,
                 pretrained,
                 down_ratio,
                 final_kernel,
                 last_level,
                 head_conv,
                 out_channel=0):
        """
        :param base_name:
        :param heads:
        :param pretrained:
        :param down_ratio:
        :param final_kernel:
        :param last_level:
        :param head_conv:
        :param out_channel:
        """
        super(DLASeg, self).__init__()

        assert down_ratio in [2, 4, 8, 16]

        self.first_level = int(np.log2(down_ratio))
        self.last_level = last_level
        self.base = globals()[base_name](pretrained=pretrained)
        channels = self.base.channels
        scales = [2 ** i for i in range(len(channels[self.first_level:]))]
        self.dla_up = DLAUp(self.first_level, channels[self.first_level:], scales)

        if out_channel == 0:
            out_channel = channels[self.first_level]

        self.ida_up = IDAUp(out_channel,
                            channels[self.first_level:self.last_level],
                            [2 ** i for i in range(self.last_level - self.first_level)])
        self.heads = heads

        self.reid_cnn = TEBM(128)
        self.hm_cnn = TDRM()

        for head in self.heads:
            out_channels = self.heads[head]  # 每个head输出的通道数
            if head_conv > 0:
                head_layer = nn.Sequential(
                    nn.Conv2d(channels[self.first_level],
                              head_conv,
                              kernel_size=3,
                              padding=1,
                              bias=True),
                    nn.ReLU(inplace=True),
                    nn.Conv2d(head_conv,
                              out_channels,
                              kernel_size=final_kernel,  # 1×1卷积
                              stride=1,
                              padding=final_kernel // 2,
                              bias=True))
                if 'hm' in head:
                    head_layer[-1].bias.data.fill_(-2.19)
                else:
                    fill_fc_weights(head_layer)
            else:
                head_layer = nn.Conv2d(channels[self.first_level],
                                       out_channels,
                                       kernel_size=final_kernel,
                                       stride=1,
                                       padding=final_kernel // 2,
                                       bias=True)
                if 'hm' in head:
                    head_layer.bias.data.fill_(-2.19)
                else:
                    fill_fc_weights(head_layer)
            # 设置输出heads属性
            self.__setattr__(head, head_layer)

    def forward(self, pre_x, x):
        # 前一帧的所有信息
        pre_x = self.base(pre_x)
        pre_x = self.dla_up(pre_x)
        pre_y = []
        for i in range(self.last_level - self.first_level):
            pre_y.append(pre_x[i].clone())
        self.ida_up(pre_y, 0, len(pre_y))

        pre_output = {}
        for head in self.heads:
            if head == 'id' or head == 'hm':
                pre_output[head] = self.__getattr__(head)(pre_y[-1])


        x = self.base(x)
        x = self.dla_up(x)
        y = []
        for i in range(self.last_level - self.first_level):
            y.append(x[i].clone())
        self.ida_up(y, 0, len(y))

        # 不计算pre_wh pre_reg的损失
        output = {}

        id_heatmap = self.__getattr__('id')(y[-1])

        enhance_id = self.reid_cnn(pre_output['id'].detach(), id_heatmap)

        pre_ID_feature = Exteact_ID_feature(pre_output)  # bxidxKx128

        # print(enhance_id.requires_grad)

        hm_corr = self.hm_cnn(pre_ID_feature, enhance_id.detach(), y[-1])

        for head in self.heads:
            if head == 'id':
                output[head] = enhance_id

            elif head == 'hm':
                output[head] = self.__getattr__(head)(hm_corr)
            else:
                output[head] = self.__getattr__(head)(y[-1])

        return [pre_output, output]


def stcmot_model(num_layers, heads, head_conv=256, down_ratio=4):
    model = DLASeg(base_name='dla{}'.format(num_layers),
                   heads=heads,
                   pretrained=True,
                   down_ratio=down_ratio,
                   final_kernel=1,  # 最后输出的head层卷积核大小: 这里用1×1的卷积改变输出的通道数
                   last_level=5,
                   head_conv=head_conv)
    return model
