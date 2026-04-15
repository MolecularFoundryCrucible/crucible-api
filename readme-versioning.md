
##### 1. Checkout a new branch and copy/create the required config files
TODO: 
```
git checkout -b v1
cp k8s-conf/configmap-main.yaml k8s-conf/configmap-v1.yaml #replace -main with -v1
cp k8s-conf/deployment-k8s-conf-main.yaml k8s-conf/deployment-k8s-conf-v1.yaml #replace -main with -v1

cp app/conf/api_conf.yaml app/conf/api_conf-v1.yaml 

```

##### 2. Deploy!
```
git add
git commit -m 'move to older version'
git push
```
The image will build on cloud build and deploy on k8s as crucible-api-vX


##### 3. Configure the networking
**The service needs its own port. Without modifying the port:**
-  -main runs on 7777
-  -vX branches will run on 7788 (allows for one prev version)
- -staging runs on 8889
** if you want to run more than one previous version then you will have to set up networking for that port**

Update a load-balancer.yaml - if you copy just update the selector app and the port to match your service. 
`kubectl apply -f load-balancer-v1.yaml` # creates/updates the service 
Update the nginx proxy to point /api/vX to the appropriate ports



