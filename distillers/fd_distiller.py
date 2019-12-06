import torch
import torch.nn as nn
from trainer import BaseTrainer

DEPTH = 1


def build_feature_connector(s_channel, t_channel):
    c_in = s_channel[0]
    h_in = s_channel[1]
    w_in = s_channel[2]
    c_out = t_channel[0]
    h_out = t_channel[1]
    w_out = t_channel[2]

    connector = []
    if h_in < h_out or w_in < w_out:
        scale = int(h_out / h_in)
        upsampler = nn.Upsample(scale_factor=scale)
        connector.append(upsampler)
    stride = int(h_in / h_out)
    conv = nn.Conv2d(c_in, c_out, kernel_size=1,
                     stride=stride, padding=0, bias=False)
    connector.append(conv)
    connector.append(nn.BatchNorm2d(c_out))
    return nn.Sequential(*connector)


def build_connectors(s_channels, t_channels):
    # channel_tuples = zip(t_channels, s_channels)
    channel_tuples = []
    for idx in range(DEPTH):
        s_channel = s_channels[idx]
        t_channel = t_channels[idx]
        channel_tuples.append((s_channel, t_channel))
    return [build_feature_connector(s, t) for s, t in channel_tuples]


def get_layer_types(feat_layers, types):
    conv_layers = []
    for layer in feat_layers:
        if not isinstance(layer, nn.Linear):
            conv_layers.append(layer)
    return conv_layers


def get_net_info(net, feats_as_module=False):
    device = next(net.parameters()).device
    if isinstance(net, nn.DataParallel):
        net = net.module
    layers = list(net.children())
    # just get the conv layers
    types = [nn.Conv2d]
    feat_layers = get_layer_types(layers, types)
    linear = layers[-1]
    channels = []
    input_size = [[3, 32, 32]]
    x = [torch.rand(2, *in_size) for in_size in input_size]
    x = torch.Tensor(*x).to(device)
    for layer in feat_layers:
        x = layer(x)
        channels.append(x.shape[1:])
    if feats_as_module:
        return nn.ModuleList(feat_layers), linear, channels
    return feat_layers, linear, channels


def get_layers(layers, lasts, x):
    layer_feats = []
    outs = []
    out = x
    for layer in layers:
        out = layer(out)
        layer_feats.append(out)
    for last in lasts:
        out = last(out)
        outs.append(out)
    return layer_feats, outs


def distillation_loss(source, target, margin):
    loss = ((source - target)**2 * (target > 0).float())
    return torch.abs(loss).sum()


def compute_last_layer(linear, last_channel):
    # assume that h_in and w_in are equal...
    c_in = last_channel[0]
    h_in = last_channel[1]
    w_in = last_channel[2]
    flat_size = c_in * h_in * w_in
    pooling = int((flat_size / linear.in_features)**(0.5))
    modules = [nn.ReLU(), nn.AvgPool2d(pooling), nn.Flatten(), linear]
    return nn.ModuleList(modules)


class Distiller(nn.Module):
    def __init__(self, s_net, t_net, batch_size=128, device="cuda"):
        super(Distiller, self).__init__()

        self.s_feat_layers, self.s_linear, s_channels = get_net_info(
            s_net, True)
        self.t_feat_layers, self.t_linear, t_channels = get_net_info(t_net)
        connectors = build_connectors(s_channels, t_channels)
        self.connectors = nn.ModuleList(connectors)

        # infer the necessary pooling based on the last feature size
        self.s_last = compute_last_layer(self.s_linear, s_channels[-1])
        self.t_last = compute_last_layer(self.t_linear, t_channels[-1])
        # freeze the teacher completely
        # for t_layer in self.t_feat_layers:
        #     t_layer.requires_grad = False
        for t_layer in self.t_last:
            t_layer.requires_grad = False

    def compute_feature_loss(self, s_feats, t_feats):
        loss_distill = 0.0
        for idx in range(DEPTH):
            t_feat = t_feats[idx]
            s_feat = s_feats[idx]
            connector = self.connectors[idx]
            s_feat = connector(s_feat)
            loss_distill += nn.MSELoss()(s_feat, t_feat)
        return loss_distill

    def forward(self, x, targets=None, is_loss=False):
        s_feats, s_outs = get_layers(self.s_feat_layers, self.s_last, x)
        t_feats, t_outs = get_layers(self.t_feat_layers, self.t_last, x)
        if is_loss:
            loss_distill = 0.0
            loss_distill += self.compute_feature_loss(s_feats, t_feats)
            return s_outs[-1], loss_distill
        return s_outs[-1]


class FDTrainer(BaseTrainer):
    def __init__(self, s_net, config):
        super(FDTrainer, self).__init__(s_net, config)

    def calculate_loss(self, data, target):
        lambda_ = self.config["lambda_student"]
        T = self.config["T_student"]
        output, loss_distill = self.net(data, target, is_loss=True)
        loss_CE = self.loss_fun(output, target)
        loss = loss_CE + loss_distill
        loss.backward()
        self.optimizer.step()
        return output, loss


def run_fd_distillation(s_net, t_net, **params):

    # Student training
    print("---------- Training FD Student -------")
    s_net = Distiller(s_net, t_net).to(params["device"])
    total_params = sum(p.numel() for p in s_net.parameters())
    print(f"FD distiller total parameters: {total_params}")
    s_trainer = FDTrainer(s_net, config=params)
    best_s_acc = s_trainer.train()

    return best_s_acc