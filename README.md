# YOLOX-BETA: support YOLOv5 & YOLOX both
To be continued...


### Baseline pretained model (check baseline branch)
|Model |size<br><sup>(pixels)|Epochs |mAP<sup>val<br>0.5:0.95 |P<br> |R<br> |params<br><sup>(M) |FLOPs<br><sup>@640 (B)
|---                    |---  |---    |---    |---    |---    |--- |---
|nano-rep      |640 |320 |**31.4**   |59.8   |46  |3.05    |7.7
|nano-rep-half-head      |640 |300 |**30.2(not finished)**   |   |  |1.83    |4.4
|yolov5n      |640 |300 |28.0   |57.4   |43.2  |1.9    |4.5

**No speed test since I don't have fancy GPUs.**


### v1.0 Ablation study
**Model size: xs & xn**
|Model |size|mAP<sup>val<br>0.5:0.95 |params<br><sup>(M) |FLOPs<br><sup>@640 (B) | Speed<br><sup>GTX1080Ti b1(ms)
|---|---|---|---|---|---
|yolov5s-silu(v6.0) 	|640 |37.4 |**7.23** |**16.53** |**7.7** 
|yolov5s-relu(v6.0) 	|640 | x   |**7.23** |**16.53** |**6.7**
|x-s-half-head-silu 	|640 |x    |7.8  |17.6 | |8.7
|x-s-half-head-relu 	|640 |x    |7.8  |17.6 | |8.0
|x-s-silu 				|640 |x    |9.0  |26.4 | |9.8
|x-s-relu 				|640 |x    |9.0  |26.4 | |9.2
|x-s-silu-v6-style 		|640 |x    |9.0  |26.4 | |11.0
|x-s-relu-v6-style 		|640 |x    |9.7  |28.6 | |9.9
|x-s-cross-conv-head 	|640 |x    |  |	| |


**yolov6 style: in one word, based on yolov5n, then doubled num of bottleneck block in backbone, they compare this model which has much bigger Params and GFLOPS to yolov5n, then comes the higher mAP results. As for inference speed, replacing all SiLU() with ReLU(). That's funny.**


...

### TODO List
	
	[x] using SiLU() anywhere
	[x] C3xESE block: C3x + ese attention
	[x] fused decoupled head: half head ??? wait to see experiments 
	[x] sa block -> increse 0.8% map in xs model =====> to test(speed)
	[x] siou
	[] Is RepConv() must be better mAP than Conv() ??  Fused RepConv() infer speed is same as fused Conv()

	[] cancel mosiac in last 20 epochs for small model.
	[] Mac calculations
	[] hyps config
	[] export rknn
	
	[] ATSS 
	[] Task-Align-Learning, TOOD
	[] end to end => NMS
	
	[] trackers
	[] pose-estimation
	[] segmentation

