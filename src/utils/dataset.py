#! /usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright 2020 Patrick Lumban Tobing (Nagoya University)
#  Apache 2.0  (http://www.apache.org/licenses/LICENSE-2.0)

import numpy as np
import torch
import os
import logging
from utils import read_hdf5, check_hdf5, write_hdf5
from torch.utils.data import Dataset
import soundfile as sf


def padding(x, flen, value=0):
    """Pad values to end by flen"""
    diff = flen - x.shape[0]
    x_len = len(x.shape)
    if diff > 0:
        if x_len > 1: #features pad replicate
            if value is not None:
                x = np.r_[x, np.ones((diff,)+x.shape[1:]) * value]
            else:
                x = np.r_[x, np.ones((diff,)+x.shape[1:]) * x[-1:]]
        else: #waveform pad zeros
            if value is not None:
                x = np.r_[x, np.ones(diff) * value]
            else:
                x = np.r_[x, np.zeros(diff)]
    return x


def validate_length(x, y, upsampling_factor=0):
    """FUNCTION TO VALIDATE LENGTH

    Args:
        x (ndarray): numpy.ndarray with x.shape[0] = len_x
        y (ndarray): numpy.ndarray with y.shape[0] = len_y
        upsampling_factor (int): upsampling factor

    Returns:
        (ndarray): length adjusted x with same length y
        (ndarray): length adjusted y with same length x
    """
    if upsampling_factor == 0:
        if x.shape[0] < y.shape[0]:
            y = y[:x.shape[0]]
        if x.shape[0] > y.shape[0]:
            x = x[:y.shape[0]]
        assert len(x) == len(y)
    else:
        mod_sample = x.shape[0] % upsampling_factor
        if mod_sample > 0:
            x = x[:-mod_sample]
        if x.shape[0] > y.shape[0] * upsampling_factor:
            x = x[:-(x.shape[0]-y.shape[0]*upsampling_factor)]
        elif x.shape[0] < y.shape[0] * upsampling_factor:
            y = y[:-((y.shape[0]*upsampling_factor-x.shape[0])//upsampling_factor)]
        assert len(x) == len(y) * upsampling_factor

    return x, y


class FeatureDatasetNeuVoco(Dataset):
    """Dataset for neural vocoder
    """

    def __init__(self, wav_list, feat_list, pad_wav_transform, pad_feat_transform, upsampling_factor, \
                    string_path, wav_transform=None, wav_transform_in=None, wav_transform_out=None):
        self.wav_list = wav_list
        self.feat_list = feat_list
        self.pad_wav_transform = pad_wav_transform
        self.pad_feat_transform = pad_feat_transform
        self.upsampling_factor = upsampling_factor
        self.string_path = string_path
        self.string_path_org = '/feat_mceplf0cap'
        #self.string_path_org = '/feat_org_lf0'
        self.wav_transform = wav_transform
        self.wav_transform_in = wav_transform_in
        self.wav_transform_out = wav_transform_out

    def __len__(self):
        return len(self.wav_list)

    def __getitem__(self, idx):
        wavfile = self.wav_list[idx]
        featfile = self.feat_list[idx]
        
        x, _ = sf.read(wavfile, dtype=np.float32)
        if check_hdf5(featfile, self.string_path):
            h = read_hdf5(featfile, self.string_path)
        else:
            h = read_hdf5(featfile, self.string_path_org)

        x, h = validate_length(x, h, self.upsampling_factor)

        if self.wav_transform_in is not None:
            x_t = self.wav_transform_in(x)
        if self.wav_transform is not None:
            if self.wav_transform_out is not None:
                x = self.wav_transform_out(self.wav_transform(x))
            else:
                x = self.wav_transform(x)
        
        slen = x.shape[0]
        flen = h.shape[0]

        h = torch.FloatTensor(self.pad_feat_transform(h))
        if self.wav_transform is not None and self.wav_transform_out is None:
            x = torch.LongTensor(self.pad_wav_transform(x))
        else:
            x = torch.FloatTensor(self.pad_wav_transform(x))

        if self.wav_transform_in is not None:
            x_t = torch.LongTensor(self.pad_wav_transform(x_t))
            return {'x_t': x_t, 'x': x, 'feat': h, 'slen': slen, 'flen': flen, 'featfile': featfile}
        else:
            return {'x': x, 'feat': h, 'slen': slen, 'flen': flen, 'featfile': featfile}


def proc_random_spkcv_statcvexcit(src_idx, spk_list, n_cv, n_frm, n_spk, stat_spk_list, mean_path, scale_path):
    mean_trg_list = [None]*n_cv
    std_trg_list = [None]*n_cv
    trg_code_list = [None]*n_cv
    pair_spk_list = [None]*n_cv
    for i in range(n_cv):
        pair_idx = np.random.randint(0,n_spk)
        while pair_idx == src_idx:
            pair_idx = np.random.randint(0,n_spk)
        trg_code_list[i] = np.ones(n_frm, dtype=np.int64)*pair_idx
        mean_trg_list[i] = read_hdf5(stat_spk_list[pair_idx], mean_path)[1:2]
        std_trg_list[i] = read_hdf5(stat_spk_list[pair_idx], scale_path)[1:2]
        pair_spk_list[i] = spk_list[pair_idx]

    return mean_trg_list, std_trg_list, trg_code_list, pair_spk_list


class FeatureDatasetCycMceplf0WavVAE(Dataset):
    """Dataset for cyclic mceplf0-waveform VAE-based VC
    """

    def __init__(self, feat_list, pad_feat_transform, spk_list, stat_spk_list, n_cyc, string_path, excit_dim=None, cap_exc_dim=None,
            upsampling_factor=None, wav_list=None, pad_wav_transform=None, wav_transform=None, logits=False, spcidx=True, uvcap_flag=True):
        self.wav_list = wav_list
        self.feat_list = feat_list
        self.pad_wav_transform = pad_wav_transform
        self.pad_feat_transform = pad_feat_transform
        self.wav_transform = wav_transform
        self.upsampling_factor = upsampling_factor
        self.stat_spk_list = stat_spk_list
        self.spk_list = spk_list
        self.n_cyc = np.max((n_cyc, 2))
        self.n_cv = int(self.n_cyc/2 + self.n_cyc%2)
        self.n_spk = len(self.spk_list)
        self.string_path = string_path
        self.logits = logits
        self.excit_dim = excit_dim
        self.cap_exc_dim = cap_exc_dim
        self.spcidx = spcidx
        if 'mel' in self.string_path:
            self.mean_path = "/mean_feat_mceplf0cap"
            self.scale_path = "/scale_feat_mceplf0cap"
            self.uvcap = False
            self.mel = True
        else:
            self.mean_path = "/mean_"+self.string_path.replace("/","")
            self.scale_path = "/scale_"+self.string_path.replace("/","")
            self.uvcap_flag = uvcap_flag
            if self.string_path == '/feat_org_lf0' and self.uvcap_flag:
                self.uvcap = True
            else:
                self.uvcap = False
            self.mel = False

    def __len__(self):
        return len(self.feat_list)

    def __getitem__(self, idx):
        featfile = self.feat_list[idx]
        #if not check_hdf5(featfile, self.string_path):
        #    logging.info('%s %s' % (featfile, self.string_path))
        if self.mel:
            if self.excit_dim is not None:
                feat = np.c_[read_hdf5(featfile, '/feat_mceplf0cap')[:,:self.excit_dim], read_hdf5(featfile, self.string_path)]
            else:
                feat = read_hdf5(featfile, self.string_path)
        else:
            if self.cap_exc_dim is None:
                feat = read_hdf5(featfile, self.string_path)
            else:
                feat = np.c_[read_hdf5(featfile, '/feat_mceplf0cap')[:,:2], read_hdf5(featfile, '/feat_mceplf0cap')[:,self.cap_exc_dim:]]
        featfile_spk = os.path.basename(os.path.dirname(featfile))
        src_idx = self.spk_list.index(featfile_spk)

        if self.spcidx:
            spcidx = read_hdf5(featfile, '/spcidx_range')[0]
            if self.wav_list is not None:
                feat = feat[:spcidx[-1]+1]
            else:
                feat = feat[spcidx[0]:spcidx[-1]+1]

        if self.wav_list is not None:
            wavfile = self.wav_list[idx]
            x, _ = sf.read(wavfile, dtype=np.float32)
            if self.spcidx:
                x = x[:-(x.shape[0]-feat.shape[0]*self.upsampling_factor)]
            x, feat = validate_length(x, feat, self.upsampling_factor)
            if self.wav_transform is not None:
                x = self.wav_transform(x)
            slen = x.shape[0]

        flen = feat.shape[0]

        mean_trg_list, std_trg_list, trg_code_list, pair_spk_list = \
            proc_random_spkcv_statcvexcit(src_idx, self.spk_list, self.n_cv, flen, self.n_spk, \
                self.stat_spk_list, self.mean_path, self.scale_path)
        if not self.mel or (self.mel and self.excit_dim is not None):
            mean_src = read_hdf5(self.stat_spk_list[src_idx], self.mean_path)[1:2]
            std_src = read_hdf5(self.stat_spk_list[src_idx], self.scale_path)[1:2]

            cv_src_list = [None]*self.n_cv
            if self.excit_dim is not None:
                if self.cap_exc_dim is None:
                    for i in range(self.n_cv):
                        cv_src_list[i] = torch.FloatTensor(self.pad_feat_transform(np.c_[feat[:,:1], \
                                            (std_trg_list[i]/std_src)*(feat[:,1:2]-mean_src)+mean_trg_list[i], feat[:,2:self.excit_dim]]))
                else:
                    for i in range(self.n_cv):
                        cv_src_list[i] = torch.FloatTensor(self.pad_feat_transform(np.c_[feat[:,:1], \
                                            (std_trg_list[i]/std_src)*(feat[:,1:2]-mean_src)+mean_trg_list[i]]))
            else:
                for i in range(self.n_cv):
                    cv_src_list[i] = torch.FloatTensor(self.pad_feat_transform(np.c_[feat[:,:1], \
                                        (std_trg_list[i]/std_src)*(feat[:,1:2]-mean_src)+mean_trg_list[i]]))

        for i in range(self.n_cv):
            trg_code_list[i] = torch.LongTensor(self.pad_feat_transform(trg_code_list[i]))

        if self.logits:
            py_logits = torch.zeros(1,self.n_spk).fill_(-103)
            py_logits[:,src_idx] = 88.72283554

        if self.uvcap:
            if self.spcidx:
                if self.wav_list is not None:
                    uvcap = read_hdf5(featfile, '/feat_mceplf0cap')[:spcidx[-1]+1,2:3]
                else:
                    uvcap = read_hdf5(featfile, '/feat_mceplf0cap')[spcidx[0]:spcidx[-1]+1,2:3]
            else:
                uvcap = read_hdf5(featfile, '/feat_mceplf0cap')[:,2:3]
            feat = torch.FloatTensor(self.pad_feat_transform(np.c_[feat,uvcap]))
        else:
            feat = torch.FloatTensor(self.pad_feat_transform(feat))
        src_codes = torch.LongTensor(self.pad_feat_transform(np.ones(flen, dtype=np.int64)*src_idx))

        if self.wav_list is None:
            if not self.logits:
                if not self.mel or (self.mel and self.excit_dim is not None):
                    return {'flen': flen, 'src_codes': src_codes, 'src_trg_codes_list': trg_code_list, \
                            'pair_spk_list': pair_spk_list, 'feat_cv_list': cv_src_list, 'featfile_spk': featfile_spk, \
                                'featfile': featfile, 'feat': feat}
                else:
                    return {'flen': flen, 'src_codes': src_codes, 'src_trg_codes_list': trg_code_list, \
                            'pair_spk_list': pair_spk_list, 'featfile_spk': featfile_spk, \
                                'featfile': featfile, 'feat': feat}
            else:
                return {'flen': flen, 'src_codes': src_codes, 'src_trg_codes_list': trg_code_list, \
                        'pair_spk_list': pair_spk_list, 'feat_cv_list': cv_src_list, 'featfile_spk': featfile_spk, \
                            'featfile': featfile, 'feat': feat, 'py_logits': py_logits}
        else:
            if self.wav_transform is not None:
                x = torch.LongTensor(self.pad_wav_transform(x))
            else:
                x = torch.FloatTensor(self.pad_wav_transform(x))

            return {'x': x, 'slen': slen, 'flen': flen, 'src_codes': src_codes, 'src_trg_codes_list': trg_code_list, \
                    'pair_spk_list': pair_spk_list, 'feat_cv_list': cv_src_list, 'featfile_spk': featfile_spk, \
                        'featfile': featfile, 'feat': feat}


class FeatureDatasetEvalCycMceplf0WavVAE(Dataset):
    """Dataset for evaluation cyclic mceplf0-waveform VAE-based VC
    """

    def __init__(self, file_list, pad_transform, spk_list, stat_spk_list, string_path, excit_dim=None, cap_exc_dim=None,
            upsampling_factor=None, wav_list=None, pad_wav_transform=None, wav_transform=None, spcidx=True, uvcap_flag=True):
        self.wav_list = wav_list
        self.file_list = file_list
        self.pad_transform = pad_transform
        self.wav_transform = wav_transform
        self.pad_wav_transform = pad_wav_transform
        self.upsampling_factor = upsampling_factor
        self.spk_list = spk_list
        self.stat_spk_list = stat_spk_list
        self.n_spk = len(self.spk_list)
        self.wav_list_src = []
        self.file_list_src = []
        self.file_list_src_trg = []
        self.list_src_trg_flag = []
        self.excit_dim = excit_dim
        self.cap_exc_dim = cap_exc_dim
        self.string_path = string_path
        self.spcidx = spcidx
        eval_exist = False
        self.uvcap_flag = uvcap_flag
        if 'mel' in self.string_path:
            self.mean_path = "/mean_feat_mceplf0cap"
            self.scale_path = "/scale_feat_mceplf0cap"
            self.uvcap = False
            self.mel = True
        else:
            self.mean_path = "/mean_"+self.string_path.replace("/","")
            self.scale_path = "/scale_"+self.string_path.replace("/","")
            self.uvcap_flag = uvcap_flag
            if self.string_path == '/feat_org_lf0' and self.uvcap_flag:
                self.uvcap = True
            else:
                self.uvcap = False
            self.mel = False
        for i in range(self.n_spk):
            if '.' not in spk_list[i] and spk_list[i].find('p') != 0 and len(self.file_list[i]) > 0:
                eval_exist = True
                break
        if eval_exist:
            # deterministically select a conv. pair for each validation utterance,
            # and deal with existence of pair data
            n_pair = self.n_spk // 2 
            #n_src = n_pair + self.n_spk % 2
            for spk_src_idx in range(self.n_spk):
                if '.' not in spk_list[spk_src_idx] and spk_list[spk_src_idx].find('p') != 0: 
                    spk_src = self.spk_list[spk_src_idx]
                    spk_src_n_utt = len(self.file_list[spk_src_idx])
                    spk_trg_idx_start = spk_src_idx + n_pair
                    if spk_trg_idx_start >= self.n_spk:
                        spk_trg_idx_start -= self.n_spk
                    while '.' in spk_list[spk_trg_idx_start] or spk_list[spk_trg_idx_start].find('p') == 0:
                        spk_trg_idx_start += 1
                    flag = False
                    for spk_trg_idx in range(spk_trg_idx_start,self.n_spk):
                        if '.' not in spk_list[spk_trg_idx] and spk_list[spk_trg_idx].find('p') != 0:
                            if spk_trg_idx != spk_src_idx:
                                spk_trg = self.spk_list[spk_trg_idx]
                                for i in range(spk_src_n_utt):
                                    file_src = self.file_list[spk_src_idx][i]
                                    if self.wav_list is not None:
                                        wav_src = self.wav_list[spk_src_idx][i]
                                    file_trg = os.path.dirname(os.path.dirname(file_src))+"/"+spk_trg+"/"+\
                                                    os.path.basename(file_src)
                                    if (file_trg in self.file_list) or os.path.exists(file_trg):
                                        self.file_list_src.append(file_src)
                                        if self.wav_list is not None:
                                            self.wav_list_src.append(wav_src)
                                        self.file_list_src_trg.append(file_trg)
                                        flag = True
                                        self.list_src_trg_flag.append(flag)
                                    elif flag:
                                        self.file_list_src.append(file_src)
                                        if self.wav_list is not None:
                                            self.wav_list_src.append(wav_src)
                                        self.file_list_src_trg.append(file_trg)
                                        self.list_src_trg_flag.append(False)
                                if flag:
                                    break
                    if not flag:
                        for spk_trg_idx in range(spk_trg_idx_start):
                            if '.' not in spk_list[spk_trg_idx] and spk_list[spk_trg_idx].find('p') != 0:
                                if spk_trg_idx != spk_src_idx:
                                    spk_trg = self.spk_list[spk_trg_idx]
                                    for i in range(spk_src_n_utt):
                                        file_src = self.file_list[spk_src_idx][i]
                                        if self.wav_list is not None:
                                            wav_src = self.wav_list[spk_src_idx][i]
                                        file_trg = os.path.dirname(os.path.dirname(file_src))+"/"+spk_trg+\
                                                                    "/"+os.path.basename(file_src)
                                        if (file_trg in self.file_list) or os.path.exists(file_trg):
                                            self.file_list_src.append(file_src)
                                            if self.wav_list is not None:
                                                self.wav_list_src.append(wav_src)
                                            self.file_list_src_trg.append(file_trg)
                                            flag = True
                                            self.list_src_trg_flag.append(flag)
                                        elif flag:
                                            self.file_list_src.append(file_src)
                                            if self.wav_list is not None:
                                                self.wav_list_src.append(wav_src)
                                            self.file_list_src_trg.append(file_trg)
                                            self.list_src_trg_flag.append(False)
                                    if flag:
                                        break
                        if not flag:
                            spk_trg = self.spk_list[spk_trg_idx_start]
                            for i in range(spk_src_n_utt):
                                file_src = self.file_list[spk_src_idx][i]
                                if self.wav_list is not None:
                                    wav_src = self.wav_list[spk_src_idx][i]
                                file_trg = os.path.dirname(os.path.dirname(file_src))+"/"+spk_trg+\
                                                            "/"+os.path.basename(file_src)
                                self.file_list_src.append(file_src)
                                if self.wav_list is not None:
                                    self.wav_list_src.append(wav_src)
                                self.file_list_src_trg.append(file_trg)
                                self.list_src_trg_flag.append(False)
        #logging.info(self.wav_list_src)
        #logging.info(self.file_list_src)

    def __len__(self):
        return len(self.file_list_src)

    def __getitem__(self, idx):
        featfile_src = self.file_list_src[idx]
        featfile_src_trg = self.file_list_src_trg[idx]
        file_src_trg_flag = self.list_src_trg_flag[idx]

        if self.mel:
            if self.excit_dim is not None:
                h_src = np.c_[read_hdf5(featfile_src, '/feat_mceplf0cap')[:,:self.excit_dim], read_hdf5(featfile_src, self.string_path)]
            else:
                h_src = read_hdf5(featfile_src, self.string_path)
        else:
            if self.cap_exc_dim is None:
                h_src = read_hdf5(featfile_src, self.string_path)
            else:
                h_src = np.c_[read_hdf5(featfile_src, '/feat_mceplf0cap')[:,:2], read_hdf5(featfile_src, '/feat_mceplf0cap')[:,self.cap_exc_dim:]]
        spk_src = os.path.basename(os.path.dirname(featfile_src))
        spk_trg = os.path.basename(os.path.dirname(featfile_src_trg))
        idx_src = self.spk_list.index(spk_src)
        idx_trg = self.spk_list.index(spk_trg)

        spcidx_src = read_hdf5(featfile_src, "/spcidx_range")[0]
        if self.spcidx:
            h_src_full = h_src
            flen_src_full = h_src_full.shape[0]
            if self.wav_list is not None:
                h_src = h_src[:spcidx_src[-1]+1]
            else:
                h_src = h_src[spcidx_src[0]:spcidx_src[-1]+1]

        if self.wav_list is not None:
            wavfile = self.wav_list_src[idx]
            x, _ = sf.read(wavfile, dtype=np.float32)
            if self.spcidx:
                x = x[:-(x.shape[0]-h_src.shape[0]*self.upsampling_factor)]
            x, h_src = validate_length(x, h_src, self.upsampling_factor)
            if self.wav_transform is not None:
                x = self.wav_transform(x)
            slen = x.shape[0]

        if not self.mel or (self.mel and self.excit_dim is not None):
            mean_src = read_hdf5(self.stat_spk_list[idx_src], self.mean_path)[1:2]
            std_src = read_hdf5(self.stat_spk_list[idx_src], self.scale_path)[1:2]
            mean_trg = read_hdf5(self.stat_spk_list[idx_trg], self.mean_path)[1:2]
            std_trg = read_hdf5(self.stat_spk_list[idx_trg], self.scale_path)[1:2]

        flen_src = h_src.shape[0]
        flen_spc_src = spcidx_src.shape[0]
        src_code = np.ones(flen_src)*idx_src
        src_trg_code = np.ones(flen_src)*idx_trg
        if self.spcidx:
            src_code_full = np.ones(flen_src_full)*idx_src
            src_trg_code_full = np.ones(flen_src_full)*idx_trg
        if not self.mel or (self.mel and self.excit_dim is not None):
            if self.excit_dim is not None:
                if self.cap_exc_dim is None:
                    cv_src = np.c_[h_src[:,:1], (std_trg/std_src)*(h_src[:,1:2]-mean_src)+mean_trg, h_src[:,2:self.excit_dim]]
                else:
                    cv_src = np.c_[h_src[:,:1], (std_trg/std_src)*(h_src[:,1:2]-mean_src)+mean_trg]
            else:
                cv_src = np.c_[h_src[:,:1], (std_trg/std_src)*(h_src[:,1:2]-mean_src)+mean_trg]

        if file_src_trg_flag:
            if self.mel:
                if self.excit_dim is not None:
                    h_src_trg = np.c_[read_hdf5(featfile_src_trg, '/feat_mceplf0cap')[:,:self.excit_dim], read_hdf5(featfile_src_trg, self.string_path)]
                else:
                    h_src_trg = read_hdf5(featfile_src_trg, self.string_path)
            else:
                if self.cap_exc_dim is None:
                    h_src_trg = read_hdf5(featfile_src_trg, self.string_path)
                else:
                    h_src_trg = np.c_[read_hdf5(featfile_src_trg, '/feat_mceplf0cap')[:,:2], read_hdf5(featfile_src_trg, '/feat_mceplf0cap')[:,self.cap_exc_dim:]]
            spcidx_src_trg = read_hdf5(featfile_src_trg, "/spcidx_range")[0]
            flen_src_trg = h_src_trg.shape[0]
            flen_spc_src_trg = spcidx_src_trg.shape[0]
            if self.uvcap:
                uvcap_trg = read_hdf5(featfile_src_trg, '/feat_mceplf0cap')[:,2:3]
                h_src_trg = torch.FloatTensor(self.pad_transform(np.c_[h_src_trg,uvcap_trg]))
            else:
                h_src_trg = torch.FloatTensor(self.pad_transform(h_src_trg))
            spcidx_src_trg = torch.LongTensor(self.pad_transform(spcidx_src_trg))

        if self.uvcap:
            if self.spcidx:
                uvcap_full = read_hdf5(featfile, '/feat_mceplf0cap')
                if self.wav_list is not None:
                    uvcap = uvcap_full[:spcidx_src[-1]+1,2:3]
                else:
                    uvcap = uvcap_full[spcidx_src[0]:spcidx_src[-1]+1,2:3]
                h_src_full = torch.FloatTensor(self.pad_transform(np.c_[h_src_full,uvcap_full]))
            else:
                uvcap = read_hdf5(featfile_src, '/feat_mceplf0cap')[:,2:3]
            h_src = torch.FloatTensor(self.pad_transform(np.c_[h_src,uvcap]))
        else:
            h_src = torch.FloatTensor(self.pad_transform(h_src))
            if self.spcidx:
                h_src_full = torch.FloatTensor(self.pad_transform(h_src_full))
        spcidx_src = torch.LongTensor(self.pad_transform(spcidx_src))
        src_code = torch.LongTensor(self.pad_transform(src_code))
        src_trg_code = torch.LongTensor(self.pad_transform(src_trg_code))
        if self.spcidx:
            src_code_full = torch.LongTensor(self.pad_transform(src_code_full))
            src_trg_code_full = torch.LongTensor(self.pad_transform(src_trg_code_full))
        if not self.mel or (self.mel and self.excit_dim is not None):
            cv_src = torch.FloatTensor(self.pad_transform(cv_src))

        if not file_src_trg_flag:
            flen_src_trg = flen_src
            flen_spc_src_trg = flen_spc_src
            h_src_trg = h_src
            spcidx_src_trg = spcidx_src

        if self.wav_list is None:
            if self.spcidx:
                if not self.mel or (self.mel and self.excit_dim is not None):
                    return {'h_src': h_src, 'flen_src': flen_src, 'src_code': src_code, 'src_trg_code': src_trg_code, \
                            'cv_src': cv_src, 'h_src_trg': h_src_trg, 'flen_src_trg': flen_src_trg, 'featfile': featfile_src, \
                            'file_src_trg_flag': file_src_trg_flag, 'spk_trg': spk_trg, 'spcidx_src': spcidx_src, \
                            'spcidx_src_trg': spcidx_src_trg, 'flen_spc_src': flen_spc_src, 'flen_spc_src_trg': flen_spc_src_trg, \
                            'h_src_full': h_src_full, 'flen_src_full': flen_src_full, \
                            'src_code_full': src_code_full, 'src_trg_code_full': src_trg_code_full}
                else:
                    return {'h_src': h_src, 'flen_src': flen_src, 'src_code': src_code, 'src_trg_code': src_trg_code, \
                            'h_src_trg': h_src_trg, 'flen_src_trg': flen_src_trg, 'featfile': featfile_src, \
                            'file_src_trg_flag': file_src_trg_flag, 'spk_trg': spk_trg, 'spcidx_src': spcidx_src, \
                            'spcidx_src_trg': spcidx_src_trg, 'flen_spc_src': flen_spc_src, 'flen_spc_src_trg': flen_spc_src_trg, \
                            'h_src_full': h_src_full, 'flen_src_full': flen_src_full, \
                            'src_code_full': src_code_full, 'src_trg_code_full': src_trg_code_full}
            else:
                return {'h_src': h_src, 'flen_src': flen_src, 'src_code': src_code, 'src_trg_code': src_trg_code, \
                        'cv_src': cv_src, 'h_src_trg': h_src_trg, 'flen_src_trg': flen_src_trg, 'featfile': featfile_src, \
                        'file_src_trg_flag': file_src_trg_flag, 'spk_trg': spk_trg, 'spcidx_src': spcidx_src, \
                        'spcidx_src_trg': spcidx_src_trg, 'flen_spc_src': flen_spc_src, 'flen_spc_src_trg': flen_spc_src_trg}
        else:
            if self.wav_transform is not None:
                x = torch.LongTensor(self.pad_wav_transform(x))
            else:
                x = torch.FloatTensor(self.pad_wav_transform(x))

            if self.spcidx:
                return {'x': x, 'slen': slen, 'h_src': h_src, 'flen_src': flen_src, 'src_code': src_code, 'src_trg_code': src_trg_code, \
                        'cv_src': cv_src, 'h_src_trg': h_src_trg, 'flen_src_trg': flen_src_trg, 'featfile': featfile_src, \
                        'file_src_trg_flag': file_src_trg_flag, 'spk_trg': spk_trg, 'spcidx_src': spcidx_src, \
                        'spcidx_src_trg': spcidx_src_trg, 'flen_spc_src': flen_spc_src, 'flen_spc_src_trg': flen_spc_src_trg, \
                        'h_src_full': h_src_full, 'flen_src_full': flen_src_full, \
                        'src_code_full': src_code_full, 'src_trg_code_full': src_trg_code_full}
            else:
                return {'x': x, 'slen': slen, 'h_src': h_src, 'flen_src': flen_src, 'src_code': src_code, 'src_trg_code': src_trg_code, \
                        'cv_src': cv_src, 'h_src_trg': h_src_trg, 'flen_src_trg': flen_src_trg, 'featfile': featfile_src, \
                        'file_src_trg_flag': file_src_trg_flag, 'spk_trg': spk_trg, 'spcidx_src': spcidx_src, \
                        'spcidx_src_trg': spcidx_src_trg, 'flen_spc_src': flen_spc_src, 'flen_spc_src_trg': flen_spc_src_trg}
