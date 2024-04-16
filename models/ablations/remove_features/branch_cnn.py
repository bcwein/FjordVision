import torch
import torch.nn as nn
import torch.nn.functional as F

class Mish(nn.Module):
    def forward(self, x):
        return x * torch.tanh(F.softplus(x))

class BranchCNN(nn.Module):
    def __init__(self, num_in_features, num_classes):
        super(BranchCNN, self).__init__()

        # Fully connected layers for processing the main features
        self.fc_layers = nn.Sequential(
            nn.Linear(num_in_features, 1024),
            nn.BatchNorm1d(1024),
            Mish(),
            nn.Dropout(0.5),
            nn.Linear(1024, 512),
            nn.BatchNorm1d(512),
            Mish(),
            nn.Dropout(0.5),
            nn.Linear(512, 256),
            nn.BatchNorm1d(256),
            Mish(),
            nn.Dropout(0.5),
            nn.Linear(256, num_classes),
        )

    def forward(self, x):
        # x should already be flattened when passed to this module
        return self.fc_layers(x)