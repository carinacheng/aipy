#!/usr/bin/python
"""
Detect and flag RFI related effects in UV files.  Uses statistical thresholding
to identify outliers in power and narrowness of features.  Does an improved
job of identifying low-level interference if sky model and crosstalk are
removed from the data first.

Author: Aaron Parsons
"""

import numpy as n, aipy as a, os, sys, pickle, optparse

o = optparse.OptionParser()
o.set_usage('xrfi.py [options] *.uv')
o.set_description(__doc__)
o.add_option('-c', '--chan', dest='chan', 
    help='Manually flag channels before xrfi processing.  Options are "<chan1 #>,..." (a list of channels), or "<chan1 #>_<chan2 #>" (a range of channels).  Default is None.')
o.add_option('-n', '--nsig', dest='nsig', default=2., type='float',
    help='Number of standard deviations above mean to flag.  Default 2.')
o.add_option('-m', '--flagmode', dest='flagmode', default='both',
    help='Can be val,int,both,none for flagging by value only, integration only, both, or only manually flagged channels.  Default both.')
o.add_option('--ch_thresh', dest='ch_thresh',type='float',default=.33,
    help='Fraction of the data in a channel which, if flagged, will result in the entire channel being flagged.  Default .33')
o.add_option('--int_thresh', dest='int_thresh',type='float',default=.99,
    help='Fraction of the data in an integration which, if flagged, will result in the entire integration being flagged.  Default .99')
o.add_option('-i', '--infile', dest='infile', action='store_true',
    help='Apply xrfi flags generated with the -o option.')
o.add_option('-o', '--outfile', dest='outfile', action='store_true',
    help='Rather than apply the flagging to the data, store them in a file (named by JD) to apply to a different file with the same JD.')
opts, args = o.parse_args(sys.argv[1:])

# Parse command-line options
uv = a.miriad.UV(args[0])
if not opts.chan is None:
    chans = a.scripting.parse_chans(opts.chan, uv['nchan'])
else:
    chans = []
    opts.chan = 'None'
del(uv)

for uvfile in args:
    uvofile = uvfile+'r'
    print uvfile,'->',uvofile
    if os.path.exists(uvofile):
        print uvofile, 'exists, skipping.'
        continue
    uvi = a.miriad.UV(uvfile)
    (uvw,jd,(i,j)),d,f = uvi.read(raw=True)
    uvi.rewind()
    if opts.infile:
        if not os.path.exists('%f.xrfi' % jd):
            print '%f.xrfi' % jd, 'does not exist.  Skipping...'
            continue
        else:
            print '    Using %f.xrfi' % jd
        f = open('%f.xrfi' % jd)
        mask = pickle.load(f)
        f.close()
        for m in mask: mask[m] = n.array(mask[m])
    else:
        # Gather all data and each time step
        window = None
        data,mask,times = {}, {}, []
        for (uvw,t,(i,j)), d, f in uvi.all(raw=True):
            if len(times) == 0 or times[-1] != t: times.append(t)
            mask[t] = mask.get(t, 0) | f
            bl = a.miriad.ij2bl(i,j)
            if i != j:
                if window is None: 
                    window = d.size/2 - abs(n.arange(d.size) - d.size/2)
                d = n.fft.fft(n.fft.ifft(d) * window)
            pol = uvi['pol']
            if not pol in data: data[pol] = {}
            if not bl in data[pol]: data[pol][bl] = {}
            data[pol][bl][t] = d

        # Manually flag data
        for bl in mask: mask[bl][chans] = 1
        # Generate a single mask for all baselines which masks data if any
        # baseline has an outlier at that freq/time.  Data flagged
        # strictly on basis of nsigma above mean.
        if opts.flagmode in ['val', 'both']:
            new_mask = {}
            for bl in mask:
                #mask[bl][chans] = 1
                new_mask[bl] = mask[bl].copy()
            for pol in data:
              for bl in data[pol]:
                i, j = a.miriad.bl2ij(bl)
                if i == j: continue
                data_times = data[pol][bl].keys()
                d = n.ma.array([data[pol][bl][t] for t in data_times],
                    mask=[mask[t] for t in data_times])
                hi_thr, lo_thr = a.rfi.gen_rfi_thresh(d, nsig=opts.nsig)
                m = n.where(n.abs(d) > hi_thr,1,0)
                for i, t in enumerate(data_times): new_mask[t] |= m[i]
            mask = new_mask
            # If more than ch_thresh of the data in a channel or 
            # integration is flagged, flag the whole thing
            ch_cnt = n.array([mask[t] for t in data_times]).sum(axis=0)
            ch_msk = n.where(ch_cnt > ch_cnt.max()*opts.ch_thresh,1,0)
            for t in mask:
                if mask[t].sum() > mask[t].size * opts.int_thresh:
                    mask[t] |= 1
                else:
                    mask[t] |= ch_msk

        # Use autocorrelations to flag entire integrations which have
        # anomalous powers.  At least 2 antennas must agree for a 
        # integration to get flagged.
        if opts.flagmode in ['int', 'both']:
            new_mask = {}
            for pol in data:
              for bl in data[pol]:
                i, j = a.miriad.bl2ij(bl)
                if i != j: continue
                data_times = data[pol][bl].keys()
                data_times.sort()
                d = n.ma.array([data[pol][bl][t] for t in data_times],
                    mask=[mask[t] for t in data_times])
                for i in n.where(a.rfi.flag_by_int(d))[0]:
                    t = data_times[i]
                    new_mask[t] = new_mask.get(t, 0) + 1
            for t in new_mask:
                if new_mask[t] > 1: mask[t] |= 1
    if opts.outfile:
        for t in mask: mask[t] = list(mask[t])
        print 'Writing %f.xrfi' % jd
        f = open('%f.xrfi' % jd, 'w')
        pickle.dump(mask, f)
        f.close()
    else:
        # Generate a pipe for applying the mask to data as it comes in.
        def rfi_mfunc(uv, preamble, data, flags):
            uvw, t, (i,j) = preamble
            return preamble, n.where(mask[t], 0, data), mask[t]

        uvi.rewind()
        uvo = a.miriad.UV(uvofile, status='new')
        uvo.init_from_uv(uvi)
        uvo.pipe(uvi, mfunc=rfi_mfunc, raw=True, append2hist='XRFI: nsig=%f chans=%s mode=%s ch_thresh=%f\n' %  (opts.nsig, opts.chan, opts.flagmode, opts.ch_thresh))
